# -*- coding: utf-8 -*-
"""R2a F5: build_shot_skeleton の beat_snap モードの単体テスト。"""
from pipeline import director


def _make_spec(cuts, telops=None, duration_sec=20.0, beat_times=None, bpm=120.0):
    return {
        "version": 2, "url": "http://x",
        "duration_sec": duration_sec,
        "transcript": "", "segments": [], "beats": [], "rhythm": None,
        "cuts": [{"t": float(t), "confidence": 0.9} for t in cuts],
        "shots_ref": [], "telops": telops or [], "sfx_events": [],
        "bgm": {"present": True, "mood_guess": "upbeat"},
        "warnings": [],
        "music": {
            "bpm": bpm,
            "beat_times": list(beat_times or []),
            "downbeats": [], "confidence": 0.85, "engine": "librosa",
        },
    }


def test_beat_snap_moves_boundaries_to_nearest_beat():
    # 参考尺 = 生成尺 = 10s。cuts=[2.15, 4.30, 6.5, 8.0]。
    # ビート @ 0.5s間隔（120BPM）。
    beats = [i * 0.5 for i in range(1, 20)]  # 0.5, 1.0, ..., 9.5
    spec = _make_spec([2.15, 4.30, 6.5, 8.0], duration_sec=10.0, beat_times=beats)
    sk_off = director.build_shot_skeleton(spec, target_duration_sec=10.0, min_shot_sec=0.5, beat_snap=False)
    sk_on = director.build_shot_skeleton(
        spec, target_duration_sec=10.0, min_shot_sec=0.5,
        beat_snap=True, beat_snap_tolerance_sec=0.25,
    )
    # off の shot 数と on の shot 数は同一（境界が動くだけ）
    assert len(sk_on["shots"]) == len(sk_off["shots"])
    # 累積境界が拍列に近くなること: 2.15 → 2.0 or 2.5、4.30 → 4.5、6.5 → 6.5、8.0 → 8.0
    def _cum(sk):
        c = 0.0
        out = [0.0]
        for s in sk["shots"]:
            c += float(s["duration_sec"])
            out.append(round(c, 3))
        return out
    cum_on = _cum(sk_on)
    # 少なくとも 2.15 相当の境界が 2.0 or 2.5 に近づいている
    # 内部境界 index 1 (=第1カット後の累積) が snap 対象
    assert abs(cum_on[1] - 2.0) < 0.06 or abs(cum_on[1] - 2.5) < 0.06
    # 合計尺は target_duration_sec
    assert abs(cum_on[-1] - 10.0) < 0.05


def test_beat_snap_respects_min_shot_sec():
    """min_shot_sec を割るスナップは棄却される。"""
    # 参考cuts に接近した2点 → shot 尺が短く、スナップで min_shot を割るケース
    beats = [1.0, 1.2, 4.0]
    spec = _make_spec([1.5, 3.7], duration_sec=6.0, beat_times=beats)
    # min_shot_sec=1.0 のとき、1.5 → 1.2 スナップは 前(0) と 0.2 = 1.2 差、後(3.7) と 2.5 差。
    # 1.2 は前境界 0.0 との差 1.2 で min_shot=1.0 を満たす → snap は成立するはず。
    sk = director.build_shot_skeleton(
        spec, target_duration_sec=6.0, min_shot_sec=1.0,
        beat_snap=True, beat_snap_tolerance_sec=0.5,
    )
    for s in sk["shots"]:
        assert s["duration_sec"] >= 1.0 - 1e-3


def test_beat_snap_no_beats_is_no_op():
    spec = _make_spec([2.0, 4.0], duration_sec=6.0, beat_times=[])
    sk_off = director.build_shot_skeleton(spec, target_duration_sec=6.0, min_shot_sec=0.5, beat_snap=False)
    sk_on = director.build_shot_skeleton(spec, target_duration_sec=6.0, min_shot_sec=0.5, beat_snap=True)
    # music.beat_times が空なら on/off で同一
    assert [s["duration_sec"] for s in sk_on["shots"]] == [s["duration_sec"] for s in sk_off["shots"]]


def test_beat_snap_scales_beats_to_target_duration():
    """参考尺=20s、生成尺=10s のとき beat_times は 1/2 にスケールしてから snap。"""
    # 参考尺 20s、beats=[2s, 6s, 10s, 14s]。cuts=[5s, 9s] → target 10s なら [2.5s, 4.5s] に相当
    beats_ref = [2.0, 6.0, 10.0, 14.0]
    spec = _make_spec([5.0, 9.0], duration_sec=20.0, beat_times=beats_ref)
    sk = director.build_shot_skeleton(
        spec, target_duration_sec=10.0, min_shot_sec=0.5,
        beat_snap=True, beat_snap_tolerance_sec=1.0,
    )
    # cuts をスケールすると 2.5s / 4.5s。beats を target-scale すると [1.0, 3.0, 5.0, 7.0]。
    # 内部境界 2.5 → 3.0 (差 0.5)、4.5 → 5.0 (差 0.5) を期待。
    c = 0.0
    cum = [0.0]
    for s in sk["shots"]:
        c += float(s["duration_sec"])
        cum.append(round(c, 3))
    assert abs(cum[1] - 3.0) < 0.06
    assert abs(cum[2] - 5.0) < 0.06
