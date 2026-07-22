# -*- coding: utf-8 -*-
"""qa/visual_inspect.py の単体テスト（純関数のみ）。

Claude vision の実呼び出しはコストのためテストしない（inspect_frames_batch は
_run_visual_inspect_dry_run と同等の性質を、resolve_caption_sample_seconds の
経由で担保する）。
"""
from qa import visual_inspect


def test_resolve_caption_sample_seconds_uses_caption_in_offset():
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 3.0, "caption_jp": "A",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 2.5},
            {"id": "s2", "duration_sec": 4.0, "caption_jp": "B",
             "caption_in_offset_sec": 1.0, "caption_out_offset_sec": 3.5},
        ]
    }
    samples = visual_inspect.resolve_caption_sample_seconds(plan, sample_offset_sec=0.3)
    assert len(samples) == 2
    # s1: shot_start=0.0 + caption_in=0.5 + 0.3 = 0.8
    assert abs(samples[0]["sample_sec"] - 0.8) < 1e-6
    # s2: shot_start=3.0 + 1.0 + 0.3 = 4.3
    assert abs(samples[1]["sample_sec"] - 4.3) < 1e-6


def test_resolve_caption_sample_seconds_skips_empty_caption():
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 3.0, "caption_jp": ""},
            {"id": "s2", "duration_sec": 4.0, "caption_jp": "B",
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 4.0},
        ]
    }
    samples = visual_inspect.resolve_caption_sample_seconds(plan)
    assert len(samples) == 1
    assert samples[0]["shot_id"] == "s2"


def test_resolve_caption_sample_seconds_clamps_within_caption_window():
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 3.0, "caption_jp": "A",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 0.6},
        ]
    }
    # in=0.5, out=0.6, offset=0.3 -> in+0.3=0.8 だが out=0.6 を超えるためクランプされる
    samples = visual_inspect.resolve_caption_sample_seconds(plan, sample_offset_sec=0.3)
    assert 0.5 < samples[0]["sample_sec"] < 0.6 + 1e-6


def test_resolve_caption_sample_seconds_scales_by_actual_duration():
    """音声主導タイミング同期モードで実mp4尺が plan.duration_sec 合計と異なる場合、
    サンプル秒を比率でスケールしないと、shot 境界を跨ぐタイミングを検査してしまう。"""
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 3.0, "caption_jp": "A",
             "caption_in_offset_sec": 1.0, "caption_out_offset_sec": 3.0},
            {"id": "s2", "duration_sec": 3.0, "caption_jp": "B",
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 3.0},
        ]
    }
    # 実mp4が3秒短い（plan合計6秒→実際3秒）→ scale=0.5
    samples = visual_inspect.resolve_caption_sample_seconds(
        plan, sample_offset_sec=0.3, actual_duration_sec=3.0,
    )
    # s1: raw=1.3, scaled=0.65
    assert abs(samples[0]["sample_sec"] - 0.65) < 1e-6
    # s2: raw=3.0+0.3=3.3, scaled=1.65
    assert abs(samples[1]["sample_sec"] - 1.65) < 1e-6


def test_resolve_caption_sample_seconds_without_caption_offset_uses_shot_center():
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 4.0, "caption_jp": "A"},
        ]
    }
    samples = visual_inspect.resolve_caption_sample_seconds(plan, sample_offset_sec=0.3)
    assert 0 < samples[0]["sample_sec"] < 4.0
