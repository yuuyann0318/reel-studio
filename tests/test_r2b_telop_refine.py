# -*- coding: utf-8 -*-
"""R2b F8: テロップ帯変化検出（純関数部）のテスト。"""
from __future__ import annotations

import pytest

from pipeline import telop_refine


class TestSnapTime:
    def test_snaps_within_tolerance(self):
        t, snapped = telop_refine._snap_time(1.0, [0.5, 0.95, 3.0], tol_sec=0.15)
        assert snapped is True
        assert abs(t - 0.95) < 1e-6

    def test_leaves_alone_when_beyond_tolerance(self):
        t, snapped = telop_refine._snap_time(1.0, [0.2, 3.5], tol_sec=0.15)
        assert snapped is False
        assert t == 1.0

    def test_empty_change_times(self):
        t, snapped = telop_refine._snap_time(1.0, [], tol_sec=0.15)
        assert snapped is False
        assert t == 1.0


class TestRefineTelops:
    def test_snaps_both_ends(self):
        telops = [{"start": 1.02, "end": 3.98, "text": "hi"}]
        change_times = [1.0, 4.0]
        refined, stats = telop_refine.refine_telops(telops, change_times, tol_sec=0.15)
        assert refined[0]["start"] == 1.0
        assert refined[0]["end"] == 4.0
        assert stats["snapped_start"] == 1
        assert stats["snapped_end"] == 1

    def test_preserves_when_no_change_close(self):
        telops = [{"start": 2.5, "end": 5.7, "text": "hi"}]
        change_times = [0.5, 8.0]  # 遠い
        refined, stats = telop_refine.refine_telops(telops, change_times, tol_sec=0.15)
        assert refined[0]["start"] == 2.5
        assert refined[0]["end"] == 5.7
        assert stats["unchanged"] == 1

    def test_end_never_less_than_start(self):
        telops = [{"start": 2.0, "end": 2.1, "text": "hi"}]
        change_times = [1.9, 2.0]  # end が start より小さくならないよう保証
        refined, _ = telop_refine.refine_telops(telops, change_times, tol_sec=0.2)
        assert refined[0]["end"] >= refined[0]["start"]

    def test_empty_telops(self):
        refined, stats = telop_refine.refine_telops([], [1.0], tol_sec=0.2)
        assert refined == []
        assert stats == {"snapped_start": 0, "snapped_end": 0, "unchanged": 0}

    def test_empty_change_times_leaves_telops_intact(self):
        telops = [{"start": 1.0, "end": 2.0, "text": "hi"}]
        refined, _ = telop_refine.refine_telops(telops, [], tol_sec=0.2)
        assert refined == telops

    def test_snaps_only_start(self):
        # start だけ近い change に落ちる
        telops = [{"start": 1.05, "end": 5.9, "text": "hi"}]
        change_times = [1.0, 10.0]
        refined, stats = telop_refine.refine_telops(telops, change_times, tol_sec=0.15)
        assert refined[0]["start"] == 1.0
        assert refined[0]["end"] == 5.9
        assert stats["snapped_start"] == 1
        assert stats["snapped_end"] == 0
