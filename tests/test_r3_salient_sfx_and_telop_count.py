# -*- coding: utf-8 -*-
"""R3: 顕著オンセット抽出 / 分布一致配置 / telop 数固定 / sfx_placement 新基準の単体テスト。

R3 目的:
  1. sfx_placement 0.056 が「参考の全 sfx_events を denom にした結果」だったため、
     参考の「重要な音」だけを抽出（select_salient_onsets）し、その集合を
     plan の sfx_plan 選定と fidelity metric の denom の**両方**で共有する。
  2. skeleton の sfx_plan は「顕著オンセットの実時刻」をアンカーに配置する
     （cut/caption_in/shot_start）— 分布一致を機械的に担保。
  3. telop 数（caption 有効 shot 数）は skeleton 固定にして LLM の勝手な削減を封じる。
"""
from __future__ import annotations

import json
import os

import pytest

from pipeline import director
from qa import fidelity


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# (1) 顕著オンセット抽出: kind 優先度 × confidence × クラスタ統合
# ---------------------------------------------------------------------------

def test_select_salient_onsets_prioritizes_transition_impact_over_shimmer():
    """同じ confidence でも kind 重みで transition/impact/riser が上位。"""
    events = [
        {"t": 0.5, "kind": "shimmer", "confidence": 0.5},   # weight = 0.25
        {"t": 5.0, "kind": "impact", "confidence": 0.5},    # weight = 0.60
        {"t": 10.0, "kind": "transition", "confidence": 0.5},  # weight = 0.70
        {"t": 15.0, "kind": "shimmer", "confidence": 0.5},  # weight = 0.25
    ]
    picked = director.select_salient_onsets(events, max_count=2)
    kinds = [p["kind"] for p in picked]
    # transition と impact が採用される（時間順で並ぶ）
    assert set(kinds) == {"impact", "transition"}


def test_select_salient_onsets_merges_close_cluster_to_single_representative():
    """近接（0.4s未満）オンセットは1クラスタに統合、最大 weight の1個だけを残す。"""
    events = [
        {"t": 1.00, "kind": "shimmer", "confidence": 0.4},  # cluster A
        {"t": 1.15, "kind": "impact",  "confidence": 0.9},  # cluster A (rep)
        {"t": 1.28, "kind": "shimmer", "confidence": 0.4},  # cluster A
        {"t": 5.00, "kind": "shimmer", "confidence": 0.5},  # cluster B (rep)
    ]
    picked = director.select_salient_onsets(events, max_count=10)
    assert len(picked) == 2, picked
    assert picked[0]["kind"] == "impact"  # cluster A の代表
    assert abs(picked[0]["t"] - 1.15) < 1e-6
    assert picked[1]["kind"] == "shimmer"
    assert abs(picked[1]["t"] - 5.00) < 1e-6


def test_select_salient_onsets_returns_time_sorted():
    events = [
        {"t": 10.0, "kind": "impact", "confidence": 0.9},
        {"t": 2.0, "kind": "transition", "confidence": 0.9},
        {"t": 5.0, "kind": "riser", "confidence": 0.9},
    ]
    picked = director.select_salient_onsets(events, max_count=10)
    ts = [p["t"] for p in picked]
    assert ts == sorted(ts)


def test_select_salient_onsets_drops_unknown_kind():
    events = [
        {"t": 1.0, "kind": "other", "confidence": 1.0},
        {"t": 2.0, "kind": "impact", "confidence": 0.5},
    ]
    picked = director.select_salient_onsets(events, max_count=10)
    assert len(picked) == 1
    assert picked[0]["kind"] == "impact"


def test_select_salient_onsets_respects_max_count():
    events = [
        {"t": float(i) * 2.0, "kind": "impact", "confidence": 0.9} for i in range(20)
    ]
    picked = director.select_salient_onsets(events, max_count=5)
    assert len(picked) == 5


def test_select_salient_onsets_on_real_spec_reduces_dense_events(real_spec):
    """実 spec（70 sfx_events）を target=25s で抽出すると 10 個程度に絞れる。"""
    # target 25s → floor(25/2.5) = 10
    picked = director.select_salient_onsets(real_spec["sfx_events"], max_count=10)
    assert 1 <= len(picked) <= 10, len(picked)
    # transition / impact / riser のいずれかが必ず含まれる（priority 効き目チェック）
    kinds = {p["kind"] for p in picked}
    assert kinds & {"transition", "impact", "riser"}, kinds


# ---------------------------------------------------------------------------
# (2) 分布一致配置: skeleton の sfx_plan がオンセットの実時刻を表現している
# ---------------------------------------------------------------------------

def test_sfx_plan_time_matches_reference_salient_onsets_within_tolerance(real_spec):
    """skeleton の sfx_plan の絶対時刻は、参考顕著オンセットを target 尺へ写像した時刻に一致する。"""
    target = 25.0
    sk = director.build_shot_skeleton(real_spec, target)
    # skeleton の shot_start 累積
    shots = sk["shots"]
    shot_start = {}
    cursor = 0.0
    for s in shots:
        shot_start[s["id"]] = cursor
        cursor += s["duration_sec"]

    # 参考顕著オンセットを target 尺へ比例スケール
    ref_dur = float(real_spec["duration_sec"])
    picked = director.select_salient_onsets(real_spec["sfx_events"],
                                             max_count=int(target / 2.5))
    # skeleton の sfx_plan の絶対時刻
    plan_times = []
    for ev in sk["sfx_plan"]:
        anc = ev["t_anchor"]
        ci_off = 0.0
        if anc["type"] == "caption_in":
            sid = anc["shot_id"]
            # 対応 shot の caption_in_offset_sec
            for s in shots:
                if s["id"] == sid:
                    ci_off = float(s.get("caption_in_offset_sec") or 0.0)
                    break
        plan_times.append(shot_start[anc["shot_id"]] + ci_off + float(anc["offset_sec"]))

    # 参考オンセットの target 空間時刻
    ref_times = [t * (target / ref_dur) for t, in [(p["t"],) for p in picked]]

    # plan.sfx_plan の件数 == 抽出上限（skeleton_max_sfx_per_shot で削れる可能性あり）
    assert len(plan_times) >= 1
    assert len(plan_times) <= len(ref_times)
    # 各 plan_time は少なくとも1つの ref_time に対して ±0.3s 以内で一致
    for pt in plan_times:
        best = min(abs(pt - rt) for rt in ref_times)
        assert best <= 0.3, "plan sfx t={:.3f} が参考顕著オンセットのどれとも ±0.3s に収まらない (best={:.3f})".format(pt, best)


def test_sfx_plan_carries_kind_from_salient_onset(real_spec):
    """skeleton の各 sfx イベントの family は、対応する顕著オンセットの kind から来ている。"""
    sk = director.build_shot_skeleton(real_spec, 25.0)
    picked = director.select_salient_onsets(real_spec["sfx_events"],
                                             max_count=int(25.0 / 2.5))
    ref_families = {director._SFX_KIND_TO_FAMILY[p["kind"]] for p in picked}
    plan_families = {ev["family"] for ev in sk["sfx_plan"]}
    # plan の family は参考顕著オンセットの family 集合の部分集合
    assert plan_families.issubset(ref_families), \
        "plan families {} ⊂ ref salient families {}".format(plan_families, ref_families)


def test_sfx_plan_anchor_types_are_snapped_to_boundary_or_shot_start():
    """cut 境界の近傍 (<0.15s) にあるオンセットは anchor=cut になる。"""
    spec = {
        "duration_sec": 20.0,
        "cuts": [{"t": 5.0}, {"t": 10.0}, {"t": 15.0}],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [
            # cut=5.0 の境界に極めて近い（<0.15）→ anchor=cut
            {"t": 5.05, "kind": "transition", "confidence": 0.9},
            # 中央部（ shot_start からも境界からも遠い）→ shot_start
            {"t": 12.5, "kind": "impact", "confidence": 0.9},
        ],
    }
    sk = director.build_shot_skeleton(spec, 20.0)
    types = [ev["t_anchor"]["type"] for ev in sk["sfx_plan"]]
    # cut anchor が最低1つある（先頭 transition が cut 境界に snap）
    assert "cut" in types


def test_sfx_plan_anchor_uses_caption_in_when_close_to_telop_start():
    """telop start の近くにあるオンセットは anchor=caption_in を使う。"""
    spec = {
        "duration_sec": 20.0,
        "cuts": [{"t": 10.0}],
        "shots_ref": [],
        "telops": [
            {"start": 3.0, "end": 8.0, "text": "テストテロップ", "position": "top",
             "style": {"color": "white", "stroke": "", "size_class": "large"}},
        ],
        "sfx_events": [
            # telop start=3.0 の直後（<0.3s）で shot 境界からは遠い → caption_in を選ぶ
            {"t": 3.10, "kind": "impact", "confidence": 0.9},
        ],
    }
    sk = director.build_shot_skeleton(spec, 20.0)
    types = [ev["t_anchor"]["type"] for ev in sk["sfx_plan"]]
    assert "caption_in" in types, types


# ---------------------------------------------------------------------------
# (3) beat_snap との整合: post-snap の shot_start を基準に offset が計算される
# ---------------------------------------------------------------------------

def test_sfx_plan_offset_consistent_with_beat_snapped_shot_starts():
    """beat_snap が境界を動かしても、sfx_plan の offset は post-snap の shot_start 起点。"""
    spec = {
        "duration_sec": 12.0,
        "cuts": [{"t": 3.9}, {"t": 7.9}],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [
            {"t": 4.0, "kind": "transition", "confidence": 0.9},
        ],
        "music": {
            "bpm": 120.0,
            "confidence": 0.9,
            "beat_times": [0.0, 4.0, 8.0, 12.0],  # 4秒刻みの拍
        },
    }
    sk = director.build_shot_skeleton(spec, 12.0, beat_snap=True, beat_snap_tolerance_sec=0.25)
    # sfx_plan の絶対時刻を再構成
    cursor = 0.0
    shot_start = {}
    for s in sk["shots"]:
        shot_start[s["id"]] = cursor
        cursor += s["duration_sec"]
    for ev in sk["sfx_plan"]:
        anc = ev["t_anchor"]
        abs_t = shot_start[anc["shot_id"]] + float(anc["offset_sec"])
        # 元の t=4.0 は beat_snap で shot 境界が 4.0 になるため、absolute time も 4.0 付近
        assert abs(abs_t - 4.0) <= 0.3, (abs_t, anc)


# ---------------------------------------------------------------------------
# (4) telop 数固定: 骨検査
# ---------------------------------------------------------------------------

def _build_plan(skel, fill_captions_for_ids):
    """指定した shot_id 集合にだけ caption を入れた plan を作る。"""
    shots = []
    for s in skel["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "placeholder"
        shot["motion_preset"] = s.get("motion_preset", "static")
        if s["id"] in fill_captions_for_ids:
            shot["caption_jp"] = "test-cap"
        else:
            shot["caption_jp"] = ""
        shot["narration_jp"] = "テストナレ"
        shots.append(shot)
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": shots, "bgm_mood": "upbeat",
        "sfx_plan": skel["sfx_plan"],
        "hook_end_shot_id": skel["hook_end_shot_id"],
        "cta_start_shot_id": skel["cta_start_shot_id"],
    }


def test_validate_plan_matches_skeleton_rejects_missing_telop(real_spec):
    """スケルトンで caption 指定のある shot に caption_jp を入れないと不合格。"""
    sk = director.build_shot_skeleton(real_spec, float(real_spec["duration_sec"]))
    # スケルトンの caption 有効 shot を取得
    caption_ids = [s["id"] for s in sk["shots"] if "caption_in_offset_sec" in s]
    # 1つ削って plan を組む
    partial = set(caption_ids[:-1])
    plan = _build_plan(sk, partial)
    errors = director._validate_plan_matches_skeleton(
        plan, sk, target_duration_sec=float(real_spec["duration_sec"])
    )
    assert any("telop" in e for e in errors), errors


def test_validate_plan_matches_skeleton_rejects_extra_telop(real_spec):
    """スケルトンで caption 指定のない shot に caption_jp を入れると不合格。"""
    sk = director.build_shot_skeleton(real_spec, float(real_spec["duration_sec"]))
    all_ids = {s["id"] for s in sk["shots"]}
    caption_ids = {s["id"] for s in sk["shots"] if "caption_in_offset_sec" in s}
    extras = (all_ids - caption_ids)
    if not extras:
        pytest.skip("参考 fixture では caption 未指定 shot がない")
    # 全 caption 指定 + 余計な shot に caption を入れる
    plan = _build_plan(sk, caption_ids | {next(iter(extras))})
    errors = director._validate_plan_matches_skeleton(
        plan, sk, target_duration_sec=float(real_spec["duration_sec"])
    )
    assert any("telop" in e for e in errors), errors


def test_validate_plan_matches_skeleton_accepts_exact_telop_match(real_spec):
    """スケルトン通りの caption 集合なら telop 検査は通る。"""
    sk = director.build_shot_skeleton(real_spec, float(real_spec["duration_sec"]))
    caption_ids = {s["id"] for s in sk["shots"] if "caption_in_offset_sec" in s}
    plan = _build_plan(sk, caption_ids)
    errors = director._validate_plan_matches_skeleton(
        plan, sk, target_duration_sec=float(real_spec["duration_sec"])
    )
    telop_errors = [e for e in errors if "telop" in e]
    assert not telop_errors, telop_errors


# ---------------------------------------------------------------------------
# (5) fidelity の新基準 sfx_placement（顕著オンセット × ±0.3s × F1）
# ---------------------------------------------------------------------------

def test_fidelity_sfx_placement_new_basis_is_higher_than_raw(real_spec):
    """skeleton そのままの plan なら、新基準 sfx_placement は raw より大きい（denom が現実的）。"""
    from tests.test_fidelity import _plan_from_skeleton
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    summary = result["summary"]
    assert summary["sfx_placement"] is not None
    assert summary["sfx_placement_raw"] is not None
    # 新基準は raw を必ず上回る（同じ plan.sfx_plan に対して denom がずっと小さい）
    assert summary["sfx_placement"] > summary["sfx_placement_raw"], summary


def test_fidelity_sfx_placement_details_expose_both_scores(real_spec):
    """details.sfx_placement に new (matched/precision/recall) と raw_detail が並記される。"""
    from tests.test_fidelity import _plan_from_skeleton
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    det = result["details"]["sfx_placement"]
    assert "score" in det and "score_raw" in det
    assert "precision" in det and "recall" in det
    assert "raw_detail" in det
    assert 0.0 <= det["score"] <= 1.0
    assert 0.0 <= det["score_raw"] <= 1.0


def test_fidelity_sfx_placement_new_basis_reaches_target_on_skeleton_only_plan(real_spec):
    """skeleton そのままの plan なら、新基準 sfx_placement は目標 0.6 以上に届く。"""
    from tests.test_fidelity import _plan_from_skeleton
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    assert result["summary"]["sfx_placement"] >= 0.6, result["summary"]


# ---------------------------------------------------------------------------
# (6) piecewise 尺スケール（beat_snap で境界がずれても telop_iou が構造的に劣化しない）
# ---------------------------------------------------------------------------

def test_fidelity_telop_iou_piecewise_scaling_survives_beat_snap():
    """beat_snap でショット境界が shift しても、piecewise 写像により telop_iou は高値を保つ。"""
    spec = {
        "duration_sec": 20.0,
        "cuts": [{"t": 5.0}, {"t": 12.0}],   # ref: [0,5], [5,12], [12,20]
        "shots_ref": [],
        "telops": [
            {"start": 1.0, "end": 3.0, "text": "a", "position": "top",
             "style": {"color": "white", "stroke": "", "size_class": "large"}},
            {"start": 6.0, "end": 10.0, "text": "b", "position": "bottom",
             "style": {"color": "yellow", "stroke": "", "size_class": "medium"}},
            {"start": 13.0, "end": 18.0, "text": "c", "position": "mid",
             "style": {"color": "white", "stroke": "", "size_class": "small"}},
        ],
        "sfx_events": [],
        "music": {
            "bpm": 120.0, "confidence": 0.9,
            "beat_times": [0.0, 4.5, 12.5, 20.0],  # 拍でショット境界を少しずらす
        },
    }
    sk = director.build_shot_skeleton(spec, 20.0, beat_snap=True, beat_snap_tolerance_sec=0.8)
    # 境界がスナップされているはず
    durs = [s["duration_sec"] for s in sk["shots"]]
    assert not (abs(durs[0] - 5.0) < 1e-3 and abs(durs[1] - 7.0) < 1e-3), \
        "テストの前提: beat_snap が境界を動かしていること"
    # skeleton のまま plan として組む
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": [dict(s, visual_prompt="p", motion_preset=s.get("motion_preset", "static"),
                        caption_jp="cap" if "caption_in_offset_sec" in s else "",
                        narration_jp="n") for s in sk["shots"]],
        "sfx_plan": sk["sfx_plan"],
        "hook_end_shot_id": sk["hook_end_shot_id"],
        "cta_start_shot_id": sk["cta_start_shot_id"],
        "bgm_mood": "upbeat",
    }
    result = fidelity.compute_fidelity(spec, plan)
    # 境界が 0.5 秒ほど動いても telop_iou は 0.85 以上を保つ（piecewise の効果）
    assert result["summary"]["telop_iou"] >= 0.85, result["summary"]


def test_fidelity_uses_shot_ref_ranges_from_plan_meta_for_piecewise():
    """R3 codex-review P2 修正: plan.meta.shot_ref_ranges があれば boundary merge / split でも
    piecewise 写像が働く。"""
    from qa.fidelity import _scale_ref_intervals_piecewise_or_linear
    # spec: 3 cuts, 4 shots [0,3][3,10][10,15][15,20]
    spec = {"duration_sec": 20.0,
            "cuts": [{"t": 3.0}, {"t": 10.0}, {"t": 15.0}], "shots_ref": []}
    # plan: shot 数は 3（boundary merge で 2 shot が統合されたシナリオ）
    plan = {
        "meta": {"shot_ref_ranges": [[0.0, 3.0], [3.0, 15.0], [15.0, 20.0]]},
        "shots": [
            {"id": "s1", "duration_sec": 5.0},
            {"id": "s2", "duration_sec": 10.0},
            {"id": "s3", "duration_sec": 5.0},
        ],
    }
    # interval (12, 14) は ref shot 2 [3, 15] 内。piecewise で target [5, 15] へ写像。
    # rel_start = (12 - 3) / (15 - 3) = 9/12 = 0.75  → 5 + 0.75 * 10 = 12.5
    # rel_end   = (14 - 3) / 12 = 11/12                → 5 + 11/12 * 10 = 14.166...
    scaled = _scale_ref_intervals_piecewise_or_linear([(12.0, 14.0)], spec, plan, 20.0, 20.0)
    assert len(scaled) == 1
    assert abs(scaled[0][0] - 12.5) < 1e-3, scaled
    assert abs(scaled[0][1] - (5 + 11/12 * 10)) < 1e-3, scaled


def test_fidelity_sfx_events_from_plan_resolves_caption_in_anchor_correctly():
    """R3 codex-review P1 修正: caption_in アンカーは
    shot_start + caption_in_offset_sec + offset_sec で解決される（sfx_planner と同じ規約）。"""
    from qa.fidelity import _sfx_events_from_plan
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "caption_in_offset_sec": 2.0},
            {"id": "s2", "duration_sec": 5.0},
        ],
        "sfx_plan": [
            # caption_in アンカーで offset 0.5 → 絶対時刻 = 0 + 2.0 + 0.5 = 2.5
            {"family": "impact", "t_anchor": {"type": "caption_in", "shot_id": "s1", "offset_sec": 0.5}},
            # shot_start アンカーで offset 1.0 → 絶対時刻 = 5.0 + 1.0 = 6.0
            {"family": "pop", "t_anchor": {"type": "shot_start", "shot_id": "s2", "offset_sec": 1.0}},
        ],
    }
    events = _sfx_events_from_plan(plan)
    assert len(events) == 2
    assert abs(events[0][0] - 2.5) < 1e-6, events[0]
    assert events[0][1] == "impact"
    assert abs(events[1][0] - 6.0) < 1e-6, events[1]
    assert events[1][1] == "pop"


def test_sfx_plan_caption_in_anchor_uses_adopted_telop_only():
    """R3 codex-review P1 修正: 同一shot に複数 telop があっても、skeleton に採用された
    先頭 telop の caption_in だけを候補にする（2枚目以降の start は候補外）。
    """
    spec = {
        "duration_sec": 10.0,
        "cuts": [],
        "shots_ref": [],
        "telops": [
            {"start": 1.0, "end": 3.0, "text": "先頭", "position": "top",
             "style": {"color": "white", "stroke": "", "size_class": "large"}},
            {"start": 5.0, "end": 8.0, "text": "2枚目 (捨てられる)", "position": "bottom",
             "style": {"color": "yellow", "stroke": "", "size_class": "medium"}},
        ],
        # 2枚目 telop の start=5.0 の近傍にオンセットを置く
        "sfx_events": [
            {"t": 5.1, "kind": "impact", "confidence": 0.9},
        ],
    }
    sk = director.build_shot_skeleton(spec, 10.0)
    # skeleton には 1 shot しかなく、その shot に採用された caption_in は先頭 telop の start=1.0
    assert len(sk["shots"]) == 1
    # sfx の anchor は「shot_start」のはず（2枚目 telop の start=5.0 を caption_in として
    # 誤採用してはいけない — 2枚目は skeleton に載っていない）
    assert len(sk["sfx_plan"]) >= 1
    for ev in sk["sfx_plan"]:
        assert ev["t_anchor"]["type"] != "caption_in", \
            "採用されていない telop の start を caption_in アンカーに使ってはいけない: {}".format(ev)


def test_fidelity_piecewise_falls_back_to_linear_when_shot_count_mismatches():
    """plan.shots 数と参考カット数（+1）が一致しない場合は線形一括スケール（後方互換）。"""
    from qa.fidelity import _scale_ref_intervals_piecewise_or_linear
    spec = {"duration_sec": 10.0, "cuts": [{"t": 5.0}], "shots_ref": []}
    plan = {"shots": [
        {"id": "s1", "duration_sec": 6.0},
        {"id": "s2", "duration_sec": 4.0},
        {"id": "s3", "duration_sec": 10.0},   # 追加 shot（cuts と件数不一致）
    ]}
    intervals = [(2.0, 4.0)]
    ref_total = 10.0
    gen_total = 20.0  # 尺 2倍
    scaled = _scale_ref_intervals_piecewise_or_linear(intervals, spec, plan, ref_total, gen_total)
    # 線形スケール: ratio=2 → (4.0, 8.0)
    assert scaled == [(4.0, 8.0)]
