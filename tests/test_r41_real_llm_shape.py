# -*- coding: utf-8 -*-
"""R4.1: 実 LLM 出力の典型崩れパターンを固定化した回帰テスト。

背景: R4 の synthetic テストは「perfect-LLM stub」で通したため、実 LLM が
skeleton 応答仕様に完全準拠できないケース(下記3種)を検出できず、実 LLM E2E
の write 段が全滅した(2026-07-25)。実 LLM が返す実際の応答から抽出した崩れ
パターンをここで固定化する。

BUG-R4-01: 複数テロップ shot(caption_slots[])で LLM は正しく captions[] を
返し caption_jp="" にするが、_validate_plan_matches_skeleton の telop 数一致
検査が caption_jp のみを数えていたため、multi-caption shot が必ず「テロップ無し」
扱いになり不合格 → 矯正リトライで LLM に「caption_jp を入れよ」と伝えるが、
プロンプトは同時に「multi-caption shot は captions[] を使い caption_jp="" にせよ」
と指示しているため収束不能。

BUG-R4-02: プロンプトの出力スキーマ節に version/meta が明記されていないため
LLM は top-level に version を出さない。矯正リトライで補正できるが1周分無駄。
_attempt_plan で version を自動注入するように修正(meta と同じパターン)。

BUG-R4-03: TTP モードで LLM が scene_id を勝手に振ると、shot ごとに異なる
reference_visual に基づく異なる visual_prompt と、plan_schema の
「同一 scene_id 内 visual_prompt 一致」制約が衝突する。skeleton モードでは
scene_id を書かないよう指示 + _attempt_plan で自動剥がす。
"""
from __future__ import annotations

from pipeline import director, plan_schema


# ---------------------------------------------------------------------------
# BUG-R4-01: multi-caption shot は captions[] を使い caption_jp="" が正しい
# ---------------------------------------------------------------------------

def test_validate_skeleton_telop_count_accepts_captions_array_with_empty_caption_jp():
    """多くの実 LLM 出力: multi-caption shot は caption_jp="" + captions[]。

    _validate_plan_matches_skeleton が caption_jp だけを見て「テロップ無し」と
    判定すると、multi-caption shot は必ず不合格になる（永久ループ）。
    """
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0,
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "caption_slots": [
                 {"caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                 {"caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
             ],
             "motion_preset": "static"},
            {"id": "s2", "duration_sec": 5.0,
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "motion_preset": "static"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0,
             "caption_jp": "",  # multi-caption shot: caption_jp は空でよい
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "captions": [
                 {"text": "テロップA", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                 {"text": "テロップB", "caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
             ],
             "motion_preset": "static"},
            {"id": "s2", "duration_sec": 5.0,
             "caption_jp": "単一テロップ本文",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "motion_preset": "static"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    errs = director._validate_plan_matches_skeleton(plan, skeleton, 10.0)
    # 「telop 数がスケルトンと不一致」エラーが出ないこと
    assert not any("telop 数" in e for e in errs), errs


def test_validate_skeleton_telop_count_rejects_multi_caption_shot_with_empty_captions_text():
    """captions[] の各 text が全部空だと telop 無しと同じで不合格。防御的な検査。"""
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0,
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "caption_slots": [
                 {"caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
             ],
             "motion_preset": "static"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "caption_jp": "",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "captions": [
                 {"text": "", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
             ],
             "motion_preset": "static"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    errs = director._validate_plan_matches_skeleton(plan, skeleton, 5.0)
    # telop 数不一致（s1 が missing）が上がる
    assert any("telop 数" in e for e in errs), errs


# ---------------------------------------------------------------------------
# BUG-R4-02: version 未指定は skeleton モードで自動注入(=2)
# ---------------------------------------------------------------------------

def test_attempt_plan_auto_injects_version_2_in_skeleton_mode(monkeypatch):
    """LLM が version を出し忘れても skeleton モードでは v2 を自動注入する。"""
    fake_llm = {
        "ok": True,
        "data": {
            # version は敢えて欠落
            "concept": "c", "hook": "h", "narration_script": "n",
            "bgm_mood": "upbeat",
            "shots": [
                {"id": "s1", "visual_prompt": "vp",
                 "duration_sec": 5.0, "caption_jp": "テロップ本文",
                 "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                 "motion_preset": "static", "narration_jp": "n"},
            ],
        },
        "model_used": "test-model", "fallback_from": None,
        "fallback_reason": None, "error": None, "attempts": [],
    }
    calls = []

    def _fake_call(prompt, timeout_sec=600, model_override=None):
        calls.append(prompt[:20])
        return fake_llm

    monkeypatch.setattr(director, "call_claude_json", _fake_call)

    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    normalized = director._attempt_plan(
        prompt="dummy", config={"claude_timeout_sec": 5},
        retries_left=0, target_duration_sec=5.0, target_tolerance_sec=8.0,
        skeleton=skeleton, reference=None,
    )
    assert normalized is not None, "auto-injection がなければ 'version は…' で不合格になる"
    assert normalized["version"] == 2
    assert normalized["meta"]["source"] == "ai"
    assert len(calls) == 1, "1回で通ることが期待値(矯正リトライは走らない)"


def test_attempt_plan_does_not_overwrite_explicit_invalid_version(monkeypatch):
    """codex-review P2 (2026-07-25): LLM が明示的に不正な version を返した場合、
    自動注入は行わず validate_plan の version エラー → 矯正リトライに委ねる。
    沈黙上書きすると schema 拡張時の互換性事故になる。
    """
    fake_llm = {
        "ok": True,
        "data": {
            "version": 3,  # 明示的に不正
            "concept": "c", "hook": "h", "narration_script": "n",
            "bgm_mood": "upbeat",
            "shots": [{"id": "s1", "visual_prompt": "vp",
                       "duration_sec": 5.0, "caption_jp": "テロップ本文",
                       "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                       "motion_preset": "static", "narration_jp": "n"}],
        },
        "model_used": "m", "fallback_from": None, "fallback_reason": None,
        "error": None, "attempts": [],
    }
    call_count = [0]

    def _fake_call(prompt, timeout_sec=600, model_override=None):
        call_count[0] += 1
        return fake_llm

    monkeypatch.setattr(director, "call_claude_json", _fake_call)
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    normalized = director._attempt_plan(
        prompt="dummy", config={"claude_timeout_sec": 5},
        retries_left=1,  # 1 回矯正リトライを許すが、fake は毎回同じ version=3 を返す → 全滅
        target_duration_sec=5.0, target_tolerance_sec=8.0,
        skeleton=skeleton, reference=None,
    )
    assert normalized is None, "明示的な不正 version は自動上書きせず、validate → 矯正 → 全滅の経路をたどるべき"
    assert call_count[0] == 2, "初回 + retries_left=1 の合計 2 回呼ばれるべき"


def test_attempt_plan_auto_injects_version_1_when_skeleton_none(monkeypatch):
    """skeleton が無いモードでは v1 を注入する（後方互換）。"""
    fake_llm = {
        "ok": True,
        "data": {
            "concept": "c", "hook": "h", "narration_script": "n",
            "bgm_mood": "upbeat",
            "shots": [{"id": "s1", "visual_prompt": "vp",
                       "duration_sec": 5.0, "caption_jp": "テロップ本文",
                       "motion_preset": "static", "narration_jp": "n"}],
        },
        "model_used": "m", "fallback_from": None, "fallback_reason": None,
        "error": None, "attempts": [],
    }

    def _fake_call(prompt, timeout_sec=600, model_override=None):
        return fake_llm

    monkeypatch.setattr(director, "call_claude_json", _fake_call)
    normalized = director._attempt_plan(
        prompt="dummy", config={"claude_timeout_sec": 5},
        retries_left=0, target_duration_sec=5.0, target_tolerance_sec=8.0,
        skeleton=None, reference=None,
    )
    assert normalized is not None
    assert normalized["version"] == 1


# ---------------------------------------------------------------------------
# BUG-R4-03: skeleton モードで LLM が scene_id を振ると剥がす
# ---------------------------------------------------------------------------

def test_attempt_plan_strips_scene_id_in_skeleton_mode(monkeypatch):
    """TTP モードで LLM が scene_id を勝手に振っても、plan_schema の scene_id 一致
    検査に衝突しないよう自動で剥がす。
    """
    fake_llm = {
        "ok": True,
        "data": {
            "version": 2,
            "concept": "c", "hook": "h", "narration_script": "a b",
            "bgm_mood": "upbeat",
            "shots": [
                {"id": "s1", "visual_prompt": "wide aerial pond",
                 "duration_sec": 5.0, "caption_jp": "one",
                 "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                 "motion_preset": "static", "narration_jp": "a",
                 "scene_id": "sc1"},  # 同 scene で異なる visual_prompt(次の shot)
                {"id": "s2", "visual_prompt": "black void with frogs",
                 "duration_sec": 5.0, "caption_jp": "two",
                 "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                 "motion_preset": "static", "narration_jp": " b",
                 "scene_id": "sc1"},
            ],
        },
        "model_used": "m", "fallback_from": None, "fallback_reason": None,
        "error": None, "attempts": [],
    }

    def _fake_call(prompt, timeout_sec=600, model_override=None):
        return fake_llm

    monkeypatch.setattr(director, "call_claude_json", _fake_call)
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
            {"id": "s2", "duration_sec": 5.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    normalized = director._attempt_plan(
        prompt="dummy", config={"claude_timeout_sec": 5},
        retries_left=0, target_duration_sec=10.0, target_tolerance_sec=8.0,
        skeleton=skeleton, reference=None,
    )
    assert normalized is not None, "scene_id 剥がしがなければ 'visual_prompt が一致していません' で不合格になる"
    for sh in normalized["shots"]:
        assert "scene_id" not in sh or sh["scene_id"] is None


def test_attempt_plan_keeps_scene_id_when_skeleton_none(monkeypatch):
    """skeleton が None（非 TTP モード）では scene_id を保持する（後方互換）。"""
    fake_llm = {
        "ok": True,
        "data": {
            "version": 1,
            "concept": "c", "hook": "h", "narration_script": "a b",
            "bgm_mood": "upbeat",
            "shots": [
                # 同一 scene_id + 同一 visual_prompt(=一致)
                {"id": "s1", "visual_prompt": "vp shared",
                 "duration_sec": 5.0, "caption_jp": "one",
                 "motion_preset": "static", "narration_jp": "a",
                 "scene_id": "sc1"},
                {"id": "s2", "visual_prompt": "vp shared",
                 "duration_sec": 5.0, "caption_jp": "two",
                 "motion_preset": "static", "narration_jp": " b",
                 "scene_id": "sc1"},
            ],
        },
        "model_used": "m", "fallback_from": None, "fallback_reason": None,
        "error": None, "attempts": [],
    }

    def _fake_call(prompt, timeout_sec=600, model_override=None):
        return fake_llm

    monkeypatch.setattr(director, "call_claude_json", _fake_call)
    normalized = director._attempt_plan(
        prompt="dummy", config={"claude_timeout_sec": 5},
        retries_left=0, target_duration_sec=10.0, target_tolerance_sec=8.0,
        skeleton=None, reference=None,
    )
    assert normalized is not None
    assert normalized["shots"][0]["scene_id"] == "sc1"
    assert normalized["shots"][1]["scene_id"] == "sc1"
