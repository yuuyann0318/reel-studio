# -*- coding: utf-8 -*-
"""R2a F1: マルチ手法カット検出のアンサンブル統合の単体テスト。"""
from pipeline import reference_v2 as v2


def test_merge_cut_lists_high_confidence_when_multiple_detectors_agree():
    # 3手法のうち 2 手法が 2.0 前後を検出、1 手法が単独で 5.0 を検出
    lists = [
        [2.0, 5.0],   # scdet 閾値低
        [2.05],       # scdet 閾値中
        [1.95, 8.0],  # ContentDetector
    ]
    events = v2.merge_cut_lists(lists, merge_window_sec=0.15)
    ts = [round(e["t"], 2) for e in events]
    # 2.0 グループ（3手法合意）と 5.0（1手法）と 8.0（1手法）が並ぶ
    assert any(abs(t - 2.0) < 0.05 for t in ts)
    assert 5.0 in ts
    assert 8.0 in ts
    # 2.0 グループの confidence は 1.0（3/3手法）
    e_20 = next(e for e in events if abs(e["t"] - 2.0) < 0.06)
    assert e_20["confidence"] == 1.0
    assert e_20["sources"] == 3
    # 5.0 は 1手法だけなので 1/3 ≈ 0.333
    e_50 = next(e for e in events if e["t"] == 5.0)
    assert 0.30 <= e_50["confidence"] <= 0.35


def test_merge_cut_lists_excludes_none_detectors_from_denominator():
    """P2: 不作動検出器(None)は分母に入れない → unanimous confidence が 1.0 を維持する。"""
    lists = [
        [2.0],        # 検出器A: 動いて 2.0 検出
        [2.02],       # 検出器B: 動いて 2.02 検出
        None,         # 検出器C: 不作動（未インストール等）
        [1.98],       # 検出器D: 動いて 1.98 検出
    ]
    events = v2.merge_cut_lists(lists, merge_window_sec=0.15)
    assert len(events) == 1
    # 3/3 active detectors 合意 → confidence 1.0
    assert events[0]["confidence"] == 1.0
    assert events[0]["sources"] == 3


def test_merge_cut_lists_all_none_returns_empty():
    assert v2.merge_cut_lists([None, None, None]) == []


def test_merge_cut_lists_empty_input_returns_empty():
    assert v2.merge_cut_lists([]) == []
    assert v2.merge_cut_lists([[]]) == []


def test_merge_cut_lists_sorted_by_time_ascending():
    lists = [[3.0, 1.0], [5.0, 2.0]]
    events = v2.merge_cut_lists(lists, merge_window_sec=0.05)
    ts = [e["t"] for e in events]
    assert ts == sorted(ts)


def test_merge_cut_lists_ignores_zero_and_negative():
    lists = [[0.0, -1.0, 2.5]]
    events = v2.merge_cut_lists(lists)
    assert [e["t"] for e in events] == [2.5]


def test_detect_cuts_via_pyscenedetect_returns_none_on_open_error():
    """P2: PySceneDetect が存在してもファイルを開けなければ **None** を返す（不作動）。
    空リストは「動いてカット0件」の意味で使うため、混同しないこと。"""
    result = v2.detect_cuts_via_pyscenedetect("/nonexistent/path.mp4")
    assert result is None
