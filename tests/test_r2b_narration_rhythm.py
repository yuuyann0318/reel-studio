# -*- coding: utf-8 -*-
"""R2b F7: ナレーションリズム統計のテスト。"""
from __future__ import annotations

from pipeline import narration_rhythm


class TestComputeNarrationRhythm:
    def test_empty_segments_returns_zeros(self):
        r = narration_rhythm.compute_narration_rhythm([], duration_sec=10.0)
        assert r["chars_per_sec"] == 0.0
        assert r["avg_gap_sec"] == 0.0
        assert r["pause_points"] == []
        assert r["segments_count"] == 0

    def test_basic_rhythm(self):
        segs = [
            {"start": 0.0, "end": 2.0, "text": "こんにちは今日は片付けの話"},  # 12字
            {"start": 2.5, "end": 4.0, "text": "5分で終わる方法があります"},   # 12字
        ]
        r = narration_rhythm.compute_narration_rhythm(segs, duration_sec=5.0, pause_min_sec=0.4)
        assert r["segments_count"] == 2
        assert r["chars_per_sec"] > 0
        assert r["avg_gap_sec"] == 0.5
        assert 2.0 in r["pause_points"]

    def test_pause_threshold_excludes_small_gaps(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "AB"},
            {"start": 1.2, "end": 2.0, "text": "CD"},  # gap 0.2s
        ]
        r = narration_rhythm.compute_narration_rhythm(segs, duration_sec=3.0, pause_min_sec=0.4)
        assert r["pause_points"] == []
        assert r["avg_gap_sec"] > 0

    def test_expected_chars_scales_by_duration(self):
        r = narration_rhythm.compute_narration_rhythm(
            [{"start": 0.0, "end": 4.0, "text": "あ" * 20}], duration_sec=4.0)
        cps = r["chars_per_sec"]
        assert narration_rhythm.expected_chars_for_shot(2.0, cps) == 10
        assert narration_rhythm.expected_chars_for_shot(0.0, cps) == 0

    def test_speech_duration_sum(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "a"},
            {"start": 2.0, "end": 3.5, "text": "bc"},
        ]
        r = narration_rhythm.compute_narration_rhythm(segs, duration_sec=5.0)
        assert r["speech_duration_sec"] == 2.5

    def test_invalid_seg_ignored(self):
        segs = [
            {"start": 0.0, "end": -0.1, "text": "bad"},  # dur<=0 は無視
            {"start": 0.0, "end": 1.0, "text": "ok"},
        ]
        r = narration_rhythm.compute_narration_rhythm(segs, duration_sec=1.0)
        assert r["speech_duration_sec"] == 1.0
