# -*- coding: utf-8 -*-
"""director.py の 3段生成 (TTP v2 移行後) を検証する。

TTP v2 Phase 2 (2026-07-21) で angles 段は撤去 (スキップ) された。
write → polish の2段生成 (quality="supreme") と write のみ (quality="single") を検証する。
"""
import json
import os

import pytest

from pipeline import director


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _plan_from_skeleton(skeleton, narration_text="オリジナルテスト用ナレーション文言。"):
    shots = []
    for s in skeleton["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "abstract test bg for {}".format(s["id"])
        shot["motion_preset"] = "static"
        shot["caption_jp"] = "テスト{}".format(s["id"])
        shot["narration_jp"] = narration_text
        shots.append(shot)
    return {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "テスト企画",
        "hook": "テストフック",
        "narration_script": narration_text * len(shots),
        "shots": shots,
        "sfx_plan": skeleton["sfx_plan"],
        "hook_end_shot_id": skeleton["hook_end_shot_id"],
        "cta_start_shot_id": skeleton["cta_start_shot_id"],
        "bgm_mood": "upbeat",
    }


def _kind(prompt):
    if "前回の出力は以下のエラー" in prompt:
        return "corrective"
    if "部下の台本" in prompt:
        return "critique"
    return "write"


# ---------------------------------------------------------------------------
# (a) supreme: write→polish の2段 (angles はスキップ済)
# ---------------------------------------------------------------------------

def test_supreme_write_polish_both_succeed_records_meta(monkeypatch, real_spec):
    kinds = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kinds.append(_kind(prompt))
        skeleton = director.build_shot_skeleton(real_spec, 20.0)
        if _kind(prompt) == "critique":
            return {"ok": True, "data": _plan_from_skeleton(skeleton, "研磨後のオリジナルナレーション"),
                    "model_used": "claude-fable-5", "error": None}
        return {"ok": True, "data": _plan_from_skeleton(skeleton, "ドラフトのオリジナルナレーション"),
                "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20.0,
                                   no_llm=False, reference=real_spec)

    assert plan["meta"]["quality"] == "supreme"
    assert plan["meta"]["source"] == "ai"
    # angles は TTP モードでスキップ済み
    assert plan["meta"]["stages"]["angles"] == {"ok": False, "skipped": "reference"}
    assert plan["meta"]["stages"]["write"]["ok"] is True
    assert plan["meta"]["stages"]["polish"]["ok"] is True
    # 最終稿は polish 段の出力
    assert plan["narration_script"].startswith("研磨後")
    assert plan["meta"]["model_used"] == "claude-fable-5"
    assert kinds == ["write", "critique"]


# ---------------------------------------------------------------------------
# (b) polish 不合格 → write のドラフトを採用
# ---------------------------------------------------------------------------

def test_polish_failure_keeps_write_draft(monkeypatch, real_spec):
    def fake_call_claude_json(prompt, timeout_sec=600):
        skeleton = director.build_shot_skeleton(real_spec, 20.0)
        k = _kind(prompt)
        # critique と、その corrective 再送はどちらも invalid を返し続けて polish を全滅させる。
        # write の corrective は起きないので "corrective" が来たら polish 側とみなす。
        if k in ("critique", "corrective"):
            return {"ok": True,
                    "data": {"version": 2, "meta": {"source": "ai"}, "concept": "c",
                              "hook": "h", "narration_script": "n", "shots": [], "bgm_mood": "upbeat"},
                    "model_used": "claude-fable-5", "error": None}
        return {"ok": True, "data": _plan_from_skeleton(skeleton, "ドラフトナレーション"),
                "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20.0,
                                   no_llm=False, reference=real_spec)

    assert plan["meta"]["stages"]["write"]["ok"] is True
    assert plan["meta"]["stages"]["polish"]["ok"] is False
    # ドラフト(write)がそのまま最終稿として採用されている
    assert plan["narration_script"].startswith("ドラフト")
    assert plan["meta"]["source"] == "ai"
    assert plan["meta"]["model_used"] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# (c) single quality: write のみ (polish もスキップ)
# ---------------------------------------------------------------------------

def test_quality_single_only_calls_write_stage(monkeypatch, real_spec):
    kinds_seen = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kinds_seen.append(_kind(prompt))
        skeleton = director.build_shot_skeleton(real_spec, 15.0)
        return {"ok": True, "data": _plan_from_skeleton(skeleton, "シングルモードのナレーション"),
                "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15.0,
                                   no_llm=False, quality="single", reference=real_spec)

    assert plan["meta"]["quality"] == "single"
    assert plan["meta"]["source"] == "ai"
    assert "stages" not in plan["meta"]
    assert kinds_seen == ["write"]


# ---------------------------------------------------------------------------
# (d) no_llm の後方互換
# ---------------------------------------------------------------------------

def test_no_llm_backward_compatible_returns_rule_based_plan():
    plan = director.run_director("AIで副業を始める", config={}, target_duration_sec=20, no_llm=True)
    assert plan["meta"]["source"] == "rule"
    assert len(plan["shots"]) >= 1
    assert "stages" not in plan["meta"]


# ---------------------------------------------------------------------------
# (e) TTP mode: reference が必須（reference=None は例外）
# ---------------------------------------------------------------------------

def test_llm_mode_requires_reference():
    with pytest.raises(director.TTPReferenceRequiredError):
        director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=False)


# ---------------------------------------------------------------------------
# (f) {ANGLE_BLOCK} 置換（残留プレースホルダが無いこと）
# ---------------------------------------------------------------------------

def test_build_director_prompt_replaces_angle_block_placeholder_when_empty():
    prompt = director.build_director_prompt("テーマ", 20, angle_block="")
    assert "{ANGLE_BLOCK}" not in prompt


def test_build_director_prompt_replaces_angle_block_placeholder_when_present():
    prompt = director.build_director_prompt("テーマ", 20, angle_block="# 採用する切り口(この戦略で書くこと)\n切り口: テスト\n")
    assert "{ANGLE_BLOCK}" not in prompt
    assert "# 採用する切り口" in prompt
    assert "切り口: テスト" in prompt


def test_build_director_prompt_default_angle_block_is_empty_and_backward_compatible():
    prompt = director.build_director_prompt("テーマ", 20)
    assert "{ANGLE_BLOCK}" not in prompt
    assert "{THEME}" not in prompt


def test_vertical_hook_style_also_replaces_angle_block_placeholder():
    prompt = director.build_director_prompt("テーマ", 15, style="vertical_hook", angle_block="切り口ブロック")
    assert "{ANGLE_BLOCK}" not in prompt
    assert "切り口ブロック" in prompt
