# -*- coding: utf-8 -*-
"""Fable 5「AIディレクター」呼び出し・矯正リトライ・決定論的フォールバック。

video-auto-editor/pipeline/director.py のパターン（claude呼び出し→plan_schema.validate_plan
で検証→不合格ならエラーを付けた矯正プロンプトで再帰リトライ→全滅時はルールベース代替へ
フォールバック）を、「編集判断」ではなく「テーマ→企画生成」向けに作り直した。

quality="supreme"（既定）では3段多段生成で企画の質を底上げする:
  1. angles  : 切り口3案を出させて1案選ばせる（Stage1）。失敗しても止めない。
  2. write   : 選ばれた切り口を踏まえて台本を書く（Stage2・従来の一発出しと同じ検証+矯正リトライ）。
  3. polish  : 書かれた台本を編集者視点でレビュー・書き直す（Stage3）。不合格ならStage2の
               ドラフトをそのまま最終稿として採用する（polish失敗で全体を落とさない）。
quality="single" は従来どおりStage2のみの一発出し。no_llm=True は完全に従来どおり。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import os

try:
    from pipeline.claude_runner import call_claude_json
except Exception:  # pragma: no cover - claude CLI周りが未整備でもimportを壊さない
    call_claude_json = None

from pipeline import plan_schema

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")

MAX_RETRIES = 2
POLISH_MAX_RETRIES = 1
DEFAULT_TARGET_TOLERANCE_SEC = 8.0
DEFAULT_QUALITY = "supreme"
QUALITY_LEVELS = ("supreme", "single")

SCHEMA_EXAMPLE = json.dumps(
    {
        "version": 1,
        "meta": {"attempt": 1, "source": "ai"},
        "concept": "AI副業の始め方を3ステップで紹介するショート動画。",
        "hook": "AIで副業、実は今日から始められます。",
        "narration_script": "AI副業について、今日は3つのポイントに絞って紹介します。1つ目は...",
        "shots": [
            {
                "id": "s1",
                "visual_prompt": "abstract geometric background representing AI and side income, soft blue gradient, clean minimal style",
                "motion_preset": "zoom_in",
                "duration_sec": 5.0,
                "caption_jp": "AI副業を始めよう",
            },
            {
                "id": "s2",
                "visual_prompt": "abstract icon representing step one, simple flat design, soft gradient background",
                "motion_preset": "pan_right",
                "duration_sec": 6.0,
                "caption_jp": "ポイント1: 全体像を知る",
            },
        ],
        "bgm_mood": "upbeat",
    },
    ensure_ascii=False,
    indent=2,
)


def _load_prompt(name):
    path = os.path.join(_PROMPT_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# スタイル名 -> プロンプトファイル名。未知のスタイル/未指定は既定(default)のプロンプトを使う。
_STYLE_PROMPT_FILES = {
    "default": "director_prompt.txt",
    "vertical_hook": "director_prompt_vertical_hook.txt",
}

_ANGLES_PROMPT_FILE = "angles_prompt.txt"
_CRITIQUE_PROMPT_FILE = "critique_prompt.txt"
_PRODUCT_BLOCK_PROMPT_FILE = "product_block.txt"

_VERTICAL_HOOK_STYLE_NOTE = "テロップは縦書き・高速カット(1カット約2秒)で見せるスタイル"


def _build_product_block(product):
    """product(dict|None) から {PRODUCT_BLOCK} 挿入用テキストを組み立てる。

    product=None（または空dict）の場合は空文字を返す（商品モードでない従来の
    企画生成は一切影響を受けない）。
    """
    if not product:
        return ""
    template = _load_prompt(_PRODUCT_BLOCK_PROMPT_FILE)
    name = (product.get("name") or "この商品").strip()
    image_count = product.get("image_count")
    try:
        image_count = int(image_count)
    except (TypeError, ValueError):
        image_count = 0
    return template.replace("{PRODUCT_NAME}", name).replace("{IMAGE_COUNT}", str(image_count))


def build_director_prompt(theme, target_duration_sec, target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC,
                           style="default", angle_block="", product=None):
    template = _load_prompt(_STYLE_PROMPT_FILES.get(style, _STYLE_PROMPT_FILES["default"]))
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{SCHEMA_EXAMPLE}", SCHEMA_EXAMPLE)
        .replace("{ANGLE_BLOCK}", angle_block or "")
        .replace("{PRODUCT_BLOCK}", _build_product_block(product))
    )


def build_corrective_prompt(base_prompt, errors):
    header = "前回の出力は以下のエラーがあり不合格でした。エラーを全て解消し、JSONのみを再出力してください。\n"
    header += "\n".join("- {}".format(e) for e in errors)
    header += "\n\n---\n\n"
    return header + base_prompt


# ---------------------------------------------------------------------------
# Stage1: angles（切り口3案生成→1案選定）
# ---------------------------------------------------------------------------

def build_angles_prompt(theme, target_duration_sec, style="default"):
    template = _load_prompt(_ANGLES_PROMPT_FILE)
    style_note = _VERTICAL_HOOK_STYLE_NOTE if style == "vertical_hook" else ""
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{STYLE_NOTE}", style_note)
    )


def _format_angle_block(candidate, chosen_reason):
    lines = [
        "# 採用する切り口(この戦略で書くこと)",
        "切り口: {}".format(candidate.get("angle", "")),
        "視点: {}".format(candidate.get("viewpoint", "")),
        "フック: {}".format(candidate.get("hook", "")),
        "感情の動き: {}".format(candidate.get("emotional_arc", "")),
        "狙い: {}".format(candidate.get("why_it_works", "")),
    ]
    if chosen_reason:
        lines.append("採用理由: {}".format(chosen_reason))
    return "\n".join(lines) + "\n"


def run_angles_stage(theme, config, target_duration_sec, style="default"):
    """Stage1: 切り口3案→1案選定。応答は緩い検証のみ行い、失敗しても止めない。

    Returns:
        (angle_block: str, stage_meta: dict)  常にこのタプルを返す（例外を投げない）。
        stage_meta = {"ok": bool, "model_used": str|None}
    """
    stage_meta = {"ok": False, "model_used": None}
    if call_claude_json is None:
        return "", stage_meta

    timeout_sec = (config or {}).get("claude_timeout_sec", 600)
    prompt = build_angles_prompt(theme, target_duration_sec, style=style)
    try:
        result = call_claude_json(prompt, timeout_sec=timeout_sec)
    except Exception:
        return "", stage_meta

    if not result:
        return "", stage_meta
    stage_meta["model_used"] = result.get("model_used")

    if not result.get("ok") or not isinstance(result.get("data"), dict):
        return "", stage_meta

    data = result["data"]
    candidates = data.get("candidates")
    chosen_index = data.get("chosen_index")
    if not isinstance(candidates, list) or not candidates:
        return "", stage_meta
    if not isinstance(chosen_index, int) or isinstance(chosen_index, bool) or not (0 <= chosen_index < len(candidates)):
        return "", stage_meta
    chosen = candidates[chosen_index]
    if not isinstance(chosen, dict):
        return "", stage_meta

    stage_meta["ok"] = True
    angle_block = _format_angle_block(chosen, data.get("chosen_reason"))
    return angle_block, stage_meta


# ---------------------------------------------------------------------------
# Stage2: write（従来の一発出し。検証+矯正リトライ）
# ---------------------------------------------------------------------------

def _attempt_plan(prompt, config, retries_left, target_duration_sec, target_tolerance_sec, trace=None):
    """claude呼び出し→バリデーション→不合格なら矯正プロンプトで再帰リトライ。全滅時は None。

    trace: 渡された場合、直近の試行の model_used を trace["last_model_used"] に記録する
    （呼び出し側が meta.stages を組み立てるための実測値取得用。戻り値の意味は変えない）。
    """
    if call_claude_json is None:
        return None
    timeout_sec = (config or {}).get("claude_timeout_sec", 600)
    try:
        result = call_claude_json(prompt, timeout_sec=timeout_sec)
    except Exception as exc:
        result = {"ok": False, "data": None, "error": str(exc)}

    if trace is not None:
        trace["last_model_used"] = (result or {}).get("model_used")

    if not result or not result.get("ok") or not isinstance(result.get("data"), dict):
        if retries_left <= 0:
            return None
        err = (result or {}).get("error") or "応答が取得できませんでした"
        corrective = build_corrective_prompt(prompt, [err])
        return _attempt_plan(corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec, trace=trace)

    candidate = dict(result["data"])
    meta = dict(candidate.get("meta") or {})
    # model_used/fallback_* はLLMの自己申告を信用せず、claude_runner側の実測値で必ず上書きする
    # （video-auto-editorで実機発覚した重大な不具合と同じ落とし穴を踏まないため）。
    meta["model_used"] = result.get("model_used")
    meta["fallback_from"] = result.get("fallback_from")
    meta["fallback_reason"] = result.get("fallback_reason")
    meta.setdefault("source", "ai")
    candidate["meta"] = meta

    ok, errors, normalized = plan_schema.validate_plan(
        candidate, target_duration_sec=target_duration_sec, target_tolerance_sec=target_tolerance_sec
    )
    if ok:
        return normalized
    if retries_left <= 0:
        return None
    corrective = build_corrective_prompt(prompt, errors)
    return _attempt_plan(corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec, trace=trace)


# ---------------------------------------------------------------------------
# Stage3: polish（編集者視点での書き直し。1回だけ矯正リトライ、全滅時はドラフト採用）
# ---------------------------------------------------------------------------

def build_critique_prompt(theme, target_duration_sec, target_tolerance_sec, style, draft_plan):
    template = _load_prompt(_CRITIQUE_PROMPT_FILE)
    draft_json = json.dumps(draft_plan, ensure_ascii=False, indent=2)
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{STYLE}", style)
        .replace("{DRAFT_JSON}", draft_json)
    )


def run_director(theme, config=None, target_duration_sec=None, no_llm=False,
                  target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC, style="default", quality=None,
                  product=None):
    """テーマから reel_plan を生成する。常に有効なplanを返す（AI失敗時はルールベース代替）。

    no_llm=True: claude呼び出しを一切行わず、決定論的テンプレートで即座に生成する
    （run.py の --no-llm オプション用）。既存呼び出し互換のため常に最優先で判定する。
    quality: "supreme"（既定。config.json の director_quality、未設定なら"supreme"）
             3段多段生成（angles→write→polish）。
             "single": 従来どおりStage2のみの一発出し。
    style: "default"（従来の企画・カット割り）| "vertical_hook"（縦書きテロップ・高速カット
    向けのTTP構成。director_prompt_vertical_hook.txt を使い、shots数=尺÷2秒目安・各ショット
    1.5〜2.5秒・5部構成のプロンプトに差し替える。claude不通時のルールベース代替も
    style別のテンプレートを使う）。
    product: 商品アフィリエイト動画モード用の商品情報 {"name","url","image_count"}。
    Noneなら従来どおり(プロンプトの{PRODUCT_BLOCK}は空文字に置換される)。ルールベース
    代替(build_rule_based_plan)は商品モードでも変更しない。
    """
    config = config or {}
    if target_duration_sec is None:
        target_duration_sec = config.get("target_duration_sec", 30)
    if quality is None:
        quality = config.get("director_quality", DEFAULT_QUALITY)
    if quality not in QUALITY_LEVELS:
        quality = DEFAULT_QUALITY

    if no_llm:
        plan = plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec, style=style)
        plan.setdefault("meta", {})
        plan["meta"]["style"] = style
        plan["meta"]["quality"] = quality
        plan["meta"]["product"] = (product or {}).get("name") if product else None
        return plan

    stages = {}
    angle_block = ""
    if quality == "supreme":
        angle_block, angles_meta = run_angles_stage(theme, config, target_duration_sec, style=style)
        stages["angles"] = angles_meta

    write_trace = {}
    prompt = build_director_prompt(
        theme, target_duration_sec, target_tolerance_sec, style=style, angle_block=angle_block, product=product
    )
    plan = _attempt_plan(prompt, config, MAX_RETRIES, target_duration_sec, target_tolerance_sec, trace=write_trace)
    stages["write"] = {"ok": plan is not None, "model_used": write_trace.get("last_model_used")}

    if plan is None:
        # 全滅（Stage2失敗）: ルールベース代替へ。この場合Stage3(polish)はスキップする。
        plan = plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec, style=style)
    elif quality == "supreme":
        polish_trace = {}
        critique_prompt = build_critique_prompt(theme, target_duration_sec, target_tolerance_sec, style, plan)
        polished = _attempt_plan(
            critique_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec, trace=polish_trace
        )
        stages["polish"] = {"ok": polished is not None, "model_used": polish_trace.get("last_model_used")}
        if polished is not None:
            plan = polished
        # else: polish不合格。Stage2のドラフト(plan)をそのまま最終稿として採用する。

    plan.setdefault("meta", {})
    plan["meta"]["style"] = style
    plan["meta"]["quality"] = quality
    plan["meta"]["product"] = (product or {}).get("name") if product else None
    if quality == "supreme":
        plan["meta"]["stages"] = stages
    return plan
