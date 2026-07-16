# -*- coding: utf-8 -*-
"""Fable 5「AIディレクター」呼び出し・矯正リトライ・決定論的フォールバック。

video-auto-editor/pipeline/director.py のパターン（claude呼び出し→plan_schema.validate_plan
で検証→不合格ならエラーを付けた矯正プロンプトで再帰リトライ→全滅時はルールベース代替へ
フォールバック）を、「編集判断」ではなく「テーマ→企画生成」向けに作り直した。

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
DEFAULT_TARGET_TOLERANCE_SEC = 8.0

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


def build_director_prompt(theme, target_duration_sec, target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC):
    template = _load_prompt("director_prompt.txt")
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{SCHEMA_EXAMPLE}", SCHEMA_EXAMPLE)
    )


def build_corrective_prompt(base_prompt, errors):
    header = "前回の出力は以下のエラーがあり不合格でした。エラーを全て解消し、JSONのみを再出力してください。\n"
    header += "\n".join("- {}".format(e) for e in errors)
    header += "\n\n---\n\n"
    return header + base_prompt


def _attempt_plan(prompt, config, retries_left, target_duration_sec, target_tolerance_sec):
    """claude呼び出し→バリデーション→不合格なら矯正プロンプトで再帰リトライ。全滅時は None。"""
    if call_claude_json is None:
        return None
    timeout_sec = (config or {}).get("claude_timeout_sec", 600)
    try:
        result = call_claude_json(prompt, timeout_sec=timeout_sec)
    except Exception as exc:
        result = {"ok": False, "data": None, "error": str(exc)}

    if not result or not result.get("ok") or not isinstance(result.get("data"), dict):
        if retries_left <= 0:
            return None
        err = (result or {}).get("error") or "応答が取得できませんでした"
        corrective = build_corrective_prompt(prompt, [err])
        return _attempt_plan(corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec)

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
    return _attempt_plan(corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec)


def run_director(theme, config=None, target_duration_sec=None, no_llm=False,
                  target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC):
    """テーマから reel_plan を生成する。常に有効なplanを返す（AI失敗時はルールベース代替）。

    no_llm=True: claude呼び出しを一切行わず、決定論的テンプレートで即座に生成する
    （run.py の --no-llm オプション用）。
    """
    config = config or {}
    if target_duration_sec is None:
        target_duration_sec = config.get("target_duration_sec", 30)

    if no_llm:
        return plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec)

    prompt = build_director_prompt(theme, target_duration_sec, target_tolerance_sec)
    plan = _attempt_plan(prompt, config, MAX_RETRIES, target_duration_sec, target_tolerance_sec)
    if plan is not None:
        return plan
    return plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec)
