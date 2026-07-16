# -*- coding: utf-8 -*-
from qa import qa_check


def test_judge_resolution_ok():
    ok, errors = qa_check.judge_resolution({"width": 1080, "height": 1920})
    assert ok is True
    assert errors == []


def test_judge_resolution_mismatch():
    ok, errors = qa_check.judge_resolution({"width": 1920, "height": 1080})
    assert ok is False
    assert len(errors) == 1


def test_judge_duration_within_tolerance():
    ok, errors = qa_check.judge_duration(31.0, 30.0, tolerance_ratio=0.15)
    assert ok is True
    assert errors == []


def test_judge_duration_outside_tolerance():
    ok, errors = qa_check.judge_duration(45.0, 30.0, tolerance_ratio=0.15)
    assert ok is False
    assert len(errors) == 1


def test_judge_duration_no_target_always_ok():
    ok, errors = qa_check.judge_duration(999.0, None)
    assert ok is True


def test_judge_has_audio_true():
    ok, errors = qa_check.judge_has_audio({"has_audio": True})
    assert ok is True


def test_judge_has_audio_false():
    ok, errors = qa_check.judge_has_audio({"has_audio": False})
    assert ok is False
    assert len(errors) == 1


def test_judge_not_all_black_zero_count_ok():
    ok, errors = qa_check.judge_not_all_black(0)
    assert ok is True


def test_judge_not_all_black_detected():
    ok, errors = qa_check.judge_not_all_black(3)
    assert ok is False
    assert "3" in errors[0]


def test_judge_file_size_positive():
    ok, errors = qa_check.judge_file_size(12345)
    assert ok is True


def test_judge_file_size_zero():
    ok, errors = qa_check.judge_file_size(0)
    assert ok is False


def test_count_black_frames_parses_multiple():
    text = "black_start:1.0 black_end:2.0\nblack_start:5.0 black_end:6.0\n"
    assert qa_check._count_black_frames(text) == 2


def test_build_probe_data_extracts_video_audio_streams():
    ffprobe_json = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920, "codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    data = qa_check._build_probe_data(ffprobe_json)
    assert data["width"] == 1080
    assert data["height"] == 1920
    assert data["has_audio"] is True
    assert data["audio_codec"] == "aac"
    assert data["duration"] == 12.5
