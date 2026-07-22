# -*- coding: utf-8 -*-
"""qa/ttp_qa.py の単体テスト。純関数 (judge_*) のみを対象。"""
import json
import os

import pytest

from qa import ttp_qa


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 段1: parse
# ---------------------------------------------------------------------------

def test_judge_parse_spec_accepts_real_v2_spec(real_spec):
    ok, errs = ttp_qa.judge_parse_spec(real_spec)
    assert ok, errs


def test_judge_parse_spec_rejects_non_monotone_cuts():
    spec = {
        "duration_sec": 20.0,
        "cuts": [{"t": 3.0}, {"t": 2.5}, {"t": 5.0}],
        "telops": [], "sfx_events": [],
    }
    ok, errs = ttp_qa.judge_parse_spec(spec)
    assert not ok
    assert any("単調非減少" in e for e in errs)


def test_judge_parse_spec_rejects_telop_out_of_range():
    spec = {
        "duration_sec": 10.0,
        "cuts": [], "sfx_events": [],
        "telops": [{"start": 1.0, "end": 11.0}],
    }
    ok, errs = ttp_qa.judge_parse_spec(spec)
    assert not ok
    assert any("telops" in e for e in errs)


def test_judge_parse_spec_rejects_hook_end_geq_cta_start():
    spec = {
        "duration_sec": 20.0, "cuts": [], "telops": [], "sfx_events": [],
        "hook_end_sec": 10.0, "cta_start_sec": 8.0,
    }
    ok, errs = ttp_qa.judge_parse_spec(spec)
    assert not ok
    assert any("hook_end_sec" in e for e in errs)


# ---------------------------------------------------------------------------
# 段2: plan
# ---------------------------------------------------------------------------

def _make_plan_from_skeleton(spec, target=30.0):
    """スケルトンをそのまま埋め字して plan にする（合格系）。"""
    from pipeline import director
    sk = director.build_shot_skeleton(spec, target)
    shots = []
    for s in sk["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "placeholder"
        shot["motion_preset"] = "static"
        shot["caption_jp"] = "テスト用テロップ"
        shot["narration_jp"] = "テスト用ナレーション"
        shots.append(shot)
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": shots, "bgm_mood": "upbeat",
        "sfx_plan": sk.get("sfx_plan") or [],
        "hook_end_shot_id": sk.get("hook_end_shot_id"),
        "cta_start_shot_id": sk.get("cta_start_shot_id"),
    }


def test_judge_plan_matches_skeleton_accepts_faithful_plan(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    ok, errs = ttp_qa.judge_plan_matches_skeleton(plan, real_spec, target_duration_sec=30.0)
    assert ok, errs


def test_judge_plan_matches_skeleton_rejects_shot_count_mismatch(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    plan["shots"].pop()  # 1件減らす
    ok, errs = ttp_qa.judge_plan_matches_skeleton(plan, real_spec, target_duration_sec=30.0)
    assert not ok
    assert any("shots 数" in e for e in errs)


def test_judge_plan_matches_skeleton_rejects_caption_offset_out_of_shot(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    plan["shots"][0]["caption_in_offset_sec"] = plan["shots"][0]["duration_sec"] + 5.0
    ok, errs = ttp_qa.judge_plan_matches_skeleton(plan, real_spec, target_duration_sec=30.0)
    assert not ok
    assert any("caption_in_offset_sec" in e for e in errs)


def test_judge_plan_matches_skeleton_rejects_sfx_shot_id_nonexistent(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    if not plan["sfx_plan"]:
        plan["sfx_plan"] = [{"t_anchor": {"type": "cut", "shot_id": "sZZ", "offset_sec": 0.0}, "family": "whoosh"}]
    else:
        plan["sfx_plan"][0]["t_anchor"]["shot_id"] = "sZZ"
    ok, errs = ttp_qa.judge_plan_matches_skeleton(plan, real_spec, target_duration_sec=30.0)
    assert not ok
    assert any("shot_id" in e for e in errs)


def test_judge_plan_matches_skeleton_rejects_over_max_shot_sec(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    ok, errs = ttp_qa.judge_plan_matches_skeleton(
        plan, real_spec, target_duration_sec=30.0, skeleton_max_shot_sec=1.0,
    )
    assert not ok
    assert any("max_shot_sec" in e for e in errs)


# ---------------------------------------------------------------------------
# 段3: render (SE presence judge)
# ---------------------------------------------------------------------------

def _mk_series(base_db=-20.0, spike_at=None, spike_db=-15.0, step=0.05, total=10.0):
    """一定 base_db の RMS 時系列（step秒間隔）に、任意で spike を差し込む。"""
    series = []
    t = 0.0
    while t < total:
        v = base_db
        if spike_at is not None and abs(t - spike_at) < step:
            v = spike_db
        series.append((round(t, 3), v))
        t += step
    return series


def test_judge_sfx_presence_ok_when_spike_over_baseline():
    events = [("whoosh", 5.0)]
    series = _mk_series(base_db=-20.0, spike_at=5.0, spike_db=-15.0)
    ok, errs, rep = ttp_qa.judge_sfx_presence(events, series, window_sec=0.5, min_delta_db=1.5)
    assert ok, (errs, rep)
    assert rep[0]["ok"]
    # 新スキーマ: window_peak_db / baseline_median_db
    assert rep[0]["window_peak_db"] is not None
    assert rep[0]["baseline_median_db"] is not None


def test_judge_sfx_presence_fail_when_no_spike():
    events = [("impact", 5.0)]
    series = _mk_series(base_db=-20.0, spike_at=None)
    ok, errs, rep = ttp_qa.judge_sfx_presence(events, series, window_sec=0.5, min_delta_db=1.5)
    assert not ok
    assert rep[0]["ok"] is False


def test_judge_sfx_presence_returns_ok_for_no_events():
    ok, errs, rep = ttp_qa.judge_sfx_presence([], _mk_series())
    assert ok
    assert rep == []


def test_judge_sfx_presence_default_threshold_is_3db_bug53():
    """BUG-53: 既定閾値が +3.0dB へ引き上げ済み。+2dB のスパイクでは不合格になる。"""
    events = [("whoosh", 5.0)]
    # narration baseline は silence floor(-30dB) より上に取る(BUG-53 silence除外を回避)。
    series = _mk_series(base_db=-20.0, spike_at=5.0, spike_db=-18.0)
    ok, errs, rep = ttp_qa.judge_sfx_presence(events, series, window_sec=0.5)  # min_delta_db=default
    assert not ok, "3dB未満のスパイクは既定閾値では不合格になるべき"
    # 逆に +4dB(=-16dB) のスパイクなら合格
    series2 = _mk_series(base_db=-20.0, spike_at=5.0, spike_db=-16.0)
    ok2, _, _ = ttp_qa.judge_sfx_presence(events, series2, window_sec=0.5)
    assert ok2


# ---------------------------------------------------------------------------
# 統合 CLI 相当（純関数だけの経路）
# ---------------------------------------------------------------------------

def test_run_ttp_qa_parse_and_plan_only(real_spec):
    plan = _make_plan_from_skeleton(real_spec, target=30.0)
    rep = ttp_qa.run_ttp_qa(plan=plan, reference_spec=real_spec, target_duration_sec=30.0)
    assert rep["overall_ok"], rep
    assert "parse" in rep["items"] and rep["items"]["parse"]["ok"]
    assert "plan" in rep["items"] and rep["items"]["plan"]["ok"]


def test_run_ttp_qa_plan_only_without_spec_skips_skeleton():
    plan = {
        "version": 1, "meta": {"source": "smoke"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "caption_jp": "x", "visual_prompt": "y",
             "motion_preset": "static"},
        ],
        "bgm_mood": "upbeat",
    }
    rep = ttp_qa.run_ttp_qa(plan=plan)
    assert rep["items"]["plan"]["ok"]
    assert "スキップ" in rep["items"]["plan"]["note"]
