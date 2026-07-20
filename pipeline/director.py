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

try:
    from pipeline.reference import find_verbatim_overlap
except Exception:  # pragma: no cover - reference.py周りが未整備でもimportを壊さない
    find_verbatim_overlap = None

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
                "scene_id": "sc1",
                "visual_prompt": "abstract geometric background representing AI and side income, soft blue gradient, clean minimal style, subject keeps moving",
                "motion_preset": "zoom_in",
                "duration_sec": 2.0,
                "caption_jp": "AI副業を始めよう",
                "narration_jp": "AI副業、実は今日から始められます。",
            },
            {
                "id": "s2",
                "scene_id": "sc1",
                "visual_prompt": "abstract geometric background representing AI and side income, soft blue gradient, clean minimal style, subject keeps moving",
                "motion_preset": "zoom_in",
                "duration_sec": 2.0,
                "caption_jp": "スマホ1台でOK",
                "narration_jp": "必要なのはスマホ1台だけ。",
            },
            {
                "id": "s3",
                "scene_id": "sc2",
                "visual_prompt": "abstract icon representing step one, simple flat design, soft gradient background",
                "motion_preset": "pan_right",
                "duration_sec": 2.0,
                "caption_jp": "ポイント1: 全体像を知る",
                "narration_jp": "まずは全体像をつかむこと。",
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
_REFERENCE_TTP_BLOCK_PROMPT_FILE = "reference_ttp_block.txt"

_VERTICAL_HOOK_STYLE_NOTE = "テロップは縦書き・高速カット(1カット約2秒)で見せるスタイル"
_VERBATIM_OVERLAP_MIN_LEN = 15


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
                           style="default", angle_block="", product=None, reference_block=""):
    template = _load_prompt(_STYLE_PROMPT_FILES.get(style, _STYLE_PROMPT_FILES["default"]))
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{SCHEMA_EXAMPLE}", SCHEMA_EXAMPLE)
        .replace("{ANGLE_BLOCK}", angle_block or "")
        .replace("{PRODUCT_BLOCK}", _build_product_block(product))
        .replace("{REFERENCE_TTP_BLOCK}", reference_block or "")
    )


def build_corrective_prompt(base_prompt, errors):
    header = "前回の出力は以下のエラーがあり不合格でした。エラーを全て解消し、JSONのみを再出力してください。\n"
    header += "\n".join("- {}".format(e) for e in errors)
    header += "\n\n---\n\n"
    return header + base_prompt


# ---------------------------------------------------------------------------
# 参考動画TTP({REFERENCE_TTP_BLOCK}): pipeline.reference.analyze_reference() が返す
# spec(ビート/リズム)を、director_prompt / director_prompt_vertical_hook / critique_prompt
# の {REFERENCE_TTP_BLOCK} に注入するテキストへ整形する。reference未指定時は常に空文字列
# （既存の企画生成挙動を一切変えない）。
# ---------------------------------------------------------------------------

def _format_beats_table(beats, ref_duration_sec, target_duration_sec):
    ref_duration_sec = ref_duration_sec or 0.0
    lines = []
    for beat in beats or []:
        start = beat.get("start", 0.0) or 0.0
        end = beat.get("end", 0.0) or 0.0
        span = max(0.0, float(end) - float(start))
        ratio = (span / ref_duration_sec) if ref_duration_sec > 0 else 0.0
        scaled_sec = ratio * target_duration_sec
        summary = beat.get("summary") or (beat.get("text") or "")[:60]
        lines.append(
            "- [{role}] 目安{sec:.1f}秒(全体の{pct:.0f}%): {summary}".format(
                role=beat.get("role", ""), sec=scaled_sec, pct=ratio * 100, summary=summary
            )
        )
    return "\n".join(lines) if lines else "(ビート情報なし)"


def _format_rhythm_summary(rhythm):
    rhythm = rhythm or {}
    endings = rhythm.get("endings") or []
    return (
        "文数: 約{sentence_count}文 / 平均文長: 約{avg:.1f}字 / 最大文長: 約{max_len:.0f}字 / "
        "トーン: {tone} / よく使う文末表現: {endings}".format(
            sentence_count=rhythm.get("sentence_count", "?"),
            avg=rhythm.get("avg_sentence_len") or 0.0,
            max_len=rhythm.get("max_sentence_len") or 0.0,
            tone=rhythm.get("tone", ""),
            endings="、".join(endings) if endings else "(不明)",
        )
    )


def _format_telops_list(telops):
    if not isinstance(telops, list) or not telops:
        return ""
    items = "\n".join("- {}".format(t) for t in telops)
    return "\n## 参考動画のテロップ例\n{}\n".format(items)


def _build_reference_block(spec, target_duration_sec):
    """analyze_referenceのspec(dict)から {REFERENCE_TTP_BLOCK} 注入用テキストを組み立てる。

    spec=None（または空dict）の場合は空文字を返す（参考なしの企画生成は一切影響を受けない）。
    """
    if not spec:
        return ""
    template = _load_prompt(_REFERENCE_TTP_BLOCK_PROMPT_FILE)
    beats_table = _format_beats_table(spec.get("beats"), spec.get("duration_sec"), target_duration_sec)
    rhythm_summary = _format_rhythm_summary(spec.get("rhythm"))
    telops_list = _format_telops_list(spec.get("telops"))
    body = (
        template.replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{BEATS_TABLE}", beats_table)
        .replace("{RHYTHM_SUMMARY}", rhythm_summary)
        .replace("{TELOPS_LIST}", telops_list)
    )
    # 呼び出し先テンプレート({ANGLE_BLOCK}{REFERENCE_TTP_BLOCK} や {STYLE}{REFERENCE_TTP_BLOCK} の
    # ように直前のプレースホルダ/テキストへ改行無しで連結される)から独立した見出しとして表示される
    # よう、先頭に改行を1つ入れる（空文字を返す reference未指定時はこの限りではない = 既存出力は不変）。
    return "\n" + body


def _check_and_retry_verbatim_overlap(plan, reference, base_prompt, config, target_duration_sec, target_tolerance_sec):
    """narration_scriptが参考動画の文字起こしと丸写しになっていないか検査し、1回だけ矯正リトライする。

    reference未指定 / plan がAI生成でない(ルールベース) / find_verbatim_overlap未使用時は何もしない。
    一致が残った場合は例外を出さず plan["meta"]["reference_overlap_warning"] に記録して plan を採用する
    （企画自体は落とさない）。
    """
    if not reference or not plan or find_verbatim_overlap is None:
        return plan
    if (plan.get("meta") or {}).get("source") != "ai":
        return plan
    reference_transcript = reference.get("transcript") if isinstance(reference, dict) else None
    if not reference_transcript:
        return plan

    overlaps = find_verbatim_overlap(
        reference_transcript, plan.get("narration_script", ""), min_len=_VERBATIM_OVERLAP_MIN_LEN
    )
    if not overlaps:
        return plan

    corrective_errors = [
        "narration_scriptに参考動画の文字起こしと{}字以上連続一致する丸写し箇所があります"
        "（著作権のため禁止。構成・リズムだけ真似て文言は自分の言葉に言い換えてください）: {}".format(
            _VERBATIM_OVERLAP_MIN_LEN, overlaps[:3]
        )
    ]
    corrective_prompt = build_corrective_prompt(base_prompt, corrective_errors)
    retried_plan = _attempt_plan(corrective_prompt, config, 0, target_duration_sec, target_tolerance_sec)

    if retried_plan is not None:
        retried_overlaps = find_verbatim_overlap(
            reference_transcript, retried_plan.get("narration_script", ""), min_len=_VERBATIM_OVERLAP_MIN_LEN
        )
        if not retried_overlaps:
            return retried_plan
        retried_plan.setdefault("meta", {})["reference_overlap_warning"] = retried_overlaps
        return retried_plan

    plan.setdefault("meta", {})["reference_overlap_warning"] = overlaps
    return plan


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

def build_critique_prompt(theme, target_duration_sec, target_tolerance_sec, style, draft_plan, reference_block=""):
    template = _load_prompt(_CRITIQUE_PROMPT_FILE)
    draft_json = json.dumps(draft_plan, ensure_ascii=False, indent=2)
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{STYLE}", style)
        .replace("{DRAFT_JSON}", draft_json)
        .replace("{REFERENCE_TTP_BLOCK}", reference_block or "")
    )


def run_director(theme, config=None, target_duration_sec=None, no_llm=False,
                  target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC, style="default", quality=None,
                  product=None, reference=None):
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
    reference: pipeline.reference.analyze_reference()が返す spec(dict、ビート/リズム/文字起こし)。
    Noneなら従来どおり(プロンプトの{REFERENCE_TTP_BLOCK}は空文字に置換され、既存の企画生成挙動を
    一切変えない)。指定時はTTP(構成の完全再現)モードになり、quality="supreme"のangles段は
    スキップする（切り口は参考動画の構成に固定されるため）。write/polish合格後にnarration_scriptが
    参考動画の文字起こしと丸写しになっていないか検査し、一致すれば1回だけ矯正リトライする
    （それでも残れば meta["reference_overlap_warning"] に記録した上でplanは採用する）。
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

    reference_block = _build_reference_block(reference, target_duration_sec) if reference else ""

    stages = {}
    angle_block = ""
    if quality == "supreme":
        if reference:
            # 参考動画のビート構成をそのまま採用するため、切り口3案生成(angles)は不要。
            stages["angles"] = {"ok": False, "skipped": "reference"}
        else:
            angle_block, angles_meta = run_angles_stage(theme, config, target_duration_sec, style=style)
            stages["angles"] = angles_meta

    write_trace = {}
    prompt = build_director_prompt(
        theme, target_duration_sec, target_tolerance_sec, style=style, angle_block=angle_block, product=product,
        reference_block=reference_block,
    )
    plan = _attempt_plan(prompt, config, MAX_RETRIES, target_duration_sec, target_tolerance_sec, trace=write_trace)
    stages["write"] = {"ok": plan is not None, "model_used": write_trace.get("last_model_used")}

    if plan is None:
        # 全滅（Stage2失敗）: ルールベース代替へ。この場合Stage3(polish)はスキップする。
        plan = plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec, style=style)
    elif quality == "supreme":
        polish_trace = {}
        critique_prompt = build_critique_prompt(
            theme, target_duration_sec, target_tolerance_sec, style, plan, reference_block=reference_block
        )
        polished = _attempt_plan(
            critique_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec, trace=polish_trace
        )
        stages["polish"] = {"ok": polished is not None, "model_used": polish_trace.get("last_model_used")}
        if polished is not None:
            plan = polished
        # else: polish不合格。Stage2のドラフト(plan)をそのまま最終稿として採用する。

    if reference:
        plan = _check_and_retry_verbatim_overlap(
            plan, reference, prompt, config, target_duration_sec, target_tolerance_sec
        )

    plan.setdefault("meta", {})
    plan["meta"]["style"] = style
    plan["meta"]["quality"] = quality
    plan["meta"]["product"] = (product or {}).get("name") if product else None
    if quality == "supreme":
        plan["meta"]["stages"] = stages
    return plan
