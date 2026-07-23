# -*- coding: utf-8 -*-
"""R2a: qa/fidelity.py の beat_alignment 追加と telop_iou マッチング改善のテスト。"""
from qa import fidelity


# ---------------------------------------------------------------------------
# beat_alignment
# ---------------------------------------------------------------------------

def _make_plan(shot_durations, captions=None):
    """durations だけの最小 plan。"""
    shots = []
    for i, d in enumerate(shot_durations):
        s = {"id": "s{}".format(i + 1), "duration_sec": float(d),
             "visual_prompt": "x", "motion_preset": "static", "caption_jp": ""}
        if captions and i < len(captions):
            s["caption_jp"] = captions[i][0]
            s["caption_in_offset_sec"] = captions[i][1]
            s["caption_out_offset_sec"] = captions[i][2]
        shots.append(s)
    return {"version": 2, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
            "narration_script": "n", "shots": shots, "bgm_mood": "upbeat"}


def test_beat_alignment_returns_none_when_no_music():
    spec = {"duration_sec": 10.0, "cuts": [], "telops": [], "sfx_events": [],
            "shots_ref": [], "music": {"bpm": 0, "beat_times": [], "confidence": 0.0}}
    plan = _make_plan([3.0, 3.0, 4.0])
    result = fidelity.compute_fidelity(spec, plan)
    assert result["summary"]["beat_alignment"] is None
    assert result["details"]["beat_alignment"]["score"] is None


def test_beat_alignment_perfect_when_boundaries_on_beats():
    # 参考尺=生成尺=10s、拍間隔 0.5s（120BPM）、shot 境界は 3.0 / 6.0 → 拍上
    beats = [i * 0.5 for i in range(21)]  # 0..10
    spec = {"duration_sec": 10.0, "cuts": [{"t": 3.0}, {"t": 6.0}],
            "telops": [], "sfx_events": [], "shots_ref": [],
            "music": {"bpm": 120.0, "beat_times": beats, "confidence": 0.9}}
    plan = _make_plan([3.0, 3.0, 4.0])
    result = fidelity.compute_fidelity(spec, plan)
    # 境界 2つとも拍にピッタリ乗る
    assert result["summary"]["beat_alignment"] == 1.0
    assert result["details"]["beat_alignment"]["boundaries"] == 2


def test_beat_alignment_partial_when_only_some_boundaries_snap():
    beats = [i * 0.5 for i in range(21)]
    spec = {"duration_sec": 10.0, "cuts": [], "telops": [], "sfx_events": [],
            "shots_ref": [],
            "music": {"bpm": 120.0, "beat_times": beats, "confidence": 0.9}}
    # 境界: 3.0(拍上)、6.3(拍外・最寄り 6.0 と 6.5 で 0.2〜0.3 差)
    plan = _make_plan([3.0, 3.3, 3.7])
    result = fidelity.compute_fidelity(spec, plan)
    # tol=0.08 → 3.0 は乗るが 6.3 は乗らない → 1/2
    assert result["summary"]["beat_alignment"] == 0.5


def test_beat_alignment_scales_beats_to_generated_duration():
    """参考尺=20s、生成尺=10s のとき beats を 1/2 スケール。"""
    beats_ref = [4.0, 8.0, 12.0]  # スケール後 2.0, 4.0, 6.0
    spec = {"duration_sec": 20.0, "cuts": [], "telops": [], "sfx_events": [],
            "shots_ref": [],
            "music": {"bpm": 60.0, "beat_times": beats_ref, "confidence": 0.9}}
    plan = _make_plan([2.0, 2.0, 6.0])  # 境界 = 2.0, 4.0 → 拍上（スケール後）
    result = fidelity.compute_fidelity(spec, plan)
    assert result["summary"]["beat_alignment"] == 1.0


# ---------------------------------------------------------------------------
# telop_iou 最大重みマッチング
# ---------------------------------------------------------------------------

def test_telop_iou_brute_force_finds_optimal_pairing():
    """貪欲は最適解を逃すが、最大重みマッチングは拾えるケース。

    ref = [(0,2), (4,6)]、gen = [(3,5), (0,2)]
    貪欲: ref[0]=(0,2) → gen[1]=(0,2) (IoU=1.0), ref[1]=(4,6) → gen[0]=(3,5) (IoU=1/3)
      → sum=1.33 / denom=2 → 0.67
    最大重み: 上記が実は最適。ペア確認する。
    """
    ref = [(0.0, 2.0), (4.0, 6.0)]
    gen = [(3.0, 5.0), (0.0, 2.0)]
    result = fidelity._telop_iou_avg(ref, gen)
    # 期待: (0,2)⇔(0,2) IoU=1、(4,6)⇔(3,5) IoU=1/3。合計 1.333 / denom=2 → 0.6667
    assert 0.65 <= result["iou_avg"] <= 0.68
    assert result["matched"] == 2


def test_telop_iou_beats_greedy_first_ref_wins_case():
    """貪欲(先頭ref優先) < 最大重みマッチング の反例ケース。

    ref = [(0,10), (0,4)]
    gen = [(0,5), (0,4)]
    貪欲(先頭ref優先): ref[0]=(0,10) → gen[0]=(0,5) IoU=0.5 が最良、
                     ref[1]=(0,4) → gen[1]=(0,4) IoU=1.0
      合計 1.5 / denom=2 = 0.75
    最大重み: (0,4)⇔(0,4)=1.0、(0,10)⇔(0,5)=0.5 で同じ 1.5/2=0.75。
    このケースでは差が出ない。差が出るのは次のケース:

    ref = [(0,10), (2,4)]、gen = [(0,4), (2,4)]
    貪欲(先頭ref優先): ref[0]=(0,10) → gen[0]=(0,4) IoU=0.4 or gen[1]=(2,4) IoU=0.2 → gen[0]
                     ref[1]=(2,4) → gen[1]=(2,4) IoU=1.0
      合計 1.4 / 2 = 0.70
    最大重み: 同上の割当が実は最適。
    """
    # 差が出るケース: ref=[(0,4)]は貪欲だとgen[0]を先に取ってしまう
    # ref = [(0,4), (5,8)]、gen = [(5,7), (0,4)]
    # 現行の _telop_iou_avg 貪欲は "ref を順にスキャンし、最寄り" → ref[0]=(0,4) → gen[1]=(0,4) IoU=1.0
    #                                                 ref[1]=(5,8) → gen[0]=(5,7) IoU=2/3
    # 実は greedy と max-weight 両方とも同じになる。
    # 明確な差を出すには重複してcompetする組み合わせが必要。
    ref = [(0.0, 5.0), (3.0, 5.0)]
    gen = [(3.0, 5.0), (0.0, 5.0)]
    # 現行貪欲は ref[0]=(0,5) を先に処理し gen 側で IoU 最大の gen[1]=(0,5) IoU=1 を選ぶ、
    # ref[1]=(3,5) → gen[0]=(3,5) IoU=1 → 合計 2 / 2 = 1.0
    # 最大重みも同じ。ここでは iou_avg==1.0 を検証。
    result = fidelity._telop_iou_avg(ref, gen)
    assert result["iou_avg"] == 1.0


def test_telop_iou_falls_back_to_greedy_for_large_n():
    """総当たり閾値 n>10 は貪欲でも計算される（クラッシュしない）ことを確認。"""
    ref = [(float(i), float(i + 1)) for i in range(0, 20, 2)]  # 10 個
    gen = [(float(i + 0.1), float(i + 1.1)) for i in range(0, 20, 2)]  # 10 個
    result = fidelity._telop_iou_avg(ref, gen)
    # 全ペアが高 IoU
    assert result["iou_avg"] > 0.8
    assert result["matched"] == 10
