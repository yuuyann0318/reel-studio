# -*- coding: utf-8 -*-
"""qa/clip_text_check.py の単体テスト（vision/ffmpeg をモック・実呼び出しなし）。"""
import os

import pytest

from qa import clip_text_check as ctc


def _touch(path):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n")
    return path


@pytest.fixture
def fake_frames(monkeypatch):
    """extract_frames_at_times を差し替え、times と同数のダミー PNG を生成して返す。"""
    def _fake(ffmpeg_bin, video_path, times_sec, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for i, _t in enumerate(times_sec):
            paths.append(_touch(os.path.join(out_dir, "f{}.png".format(i))))
        return paths
    monkeypatch.setattr(ctc, "extract_frames_at_times", _fake)


def _vision_ok(verdict_map):
    """frame_index -> {"has_text","garbled","readable","note"} を返す fake vision 関数を作る。

    prompt に埋め込まれた shot_id は使わず frame_index の順序で判定を返す。
    """
    def _fn(prompt, paths, timeout_sec=600):
        data = []
        for i in range(len(paths)):
            v = dict(verdict_map.get(i, {"has_text": False}))
            v["frame_index"] = i
            data.append(v)
        return {"ok": True, "data": data, "model_used": "fake-model", "error": None}
    return _fn


def _clip(shot_id, path, dur=5.0):
    return {"shot_id": shot_id, "clip_path": _touch(path), "duration_sec": dur}


def test_disabled_returns_empty(tmp_path, fake_frames):
    cfg = {"visual": {"text_check": {"enabled": False}}}
    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))], ffmpeg_bin="ffmpeg", cfg=cfg,
        tmp_dir=str(tmp_path / "t"), _vision_fn=_vision_ok({}),
    )
    assert out["enabled"] is False
    assert out["text_artifacts"] == []
    assert out["calls_used"] == 0


def test_clean_clips_produce_no_artifacts(tmp_path, fake_frames):
    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4")), _clip("s2", str(tmp_path / "s2.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"),
        _vision_fn=_vision_ok({}),  # 全フレーム has_text=False
    )
    assert out["ok"] is True
    assert out["text_artifacts"] == []
    assert set(out["checked_shot_ids"]) == {"s1", "s2"}


def test_garbled_text_detected_as_garbled(tmp_path, fake_frames):
    # s1 は 2 フレーム抽出（先頭+中央）。2枚目に崩れ文字あり。
    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"),
        _vision_fn=_vision_ok({
            0: {"has_text": False},
            1: {"has_text": True, "garbled": True, "readable": False, "note": "崩れた日本語風の文字"},
        }),
    )
    arts = out["text_artifacts"]
    assert len(arts) == 1
    assert arts[0]["shot_id"] == "s1"
    assert arts[0]["verdict"] == "garbled"
    assert "崩れた" in arts[0]["note"]


def test_readable_text_detected_as_text_not_garbled(tmp_path, fake_frames):
    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"),
        _vision_fn=_vision_ok({
            0: {"has_text": True, "garbled": False, "readable": True, "note": "ロゴ文字"},
            1: {"has_text": False},
        }),
    )
    arts = out["text_artifacts"]
    assert len(arts) == 1
    assert arts[0]["verdict"] == "text"


def test_max_vision_calls_cap_marks_uncovered_shots_unchecked(tmp_path, fake_frames):
    # batch_size=1, max_calls=1 → 1フレームしか送らない。3フレーム抽出の各ショットは
    # 判定が揃わず「未検査」になり、クリーンとは報告しない（codex 指摘対応）。
    cfg = {"visual": {"text_check": {"max_vision_calls": 1, "vision_batch_size": 1}}}
    calls = {"n": 0}

    def _counting(prompt, paths, timeout_sec=600):
        calls["n"] += 1
        return {"ok": True, "data": [{"frame_index": 0, "has_text": True, "garbled": True, "note": "x"}],
                "model_used": "m", "error": None}

    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4")), _clip("s2", str(tmp_path / "s2.mp4"))],
        ffmpeg_bin="ffmpeg", cfg=cfg, tmp_dir=str(tmp_path / "t"), _vision_fn=_counting,
    )
    assert out["calls_used"] == 1
    assert calls["n"] == 1
    assert out["checked_shot_ids"] == []
    assert set(out["unchecked_shot_ids"]) == {"s1", "s2"}
    assert out["text_artifacts"] == []
    assert out["ok"] is False


def test_incomplete_vision_response_marks_shot_unchecked(tmp_path, fake_frames):
    # s1 は3フレーム抽出されるが vision が2件しか返さない → 判定不揃い → 未検査（クリーンと誤断しない）。
    def _partial(prompt, paths, timeout_sec=600):
        data = [{"frame_index": 0, "has_text": True, "garbled": True, "note": "x"},
                {"frame_index": 1, "has_text": False}]  # frame_index=2 欠落
        return {"ok": True, "data": data, "model_used": "m", "error": None}

    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"), _vision_fn=_partial,
    )
    assert out["checked_shot_ids"] == []
    assert out["unchecked_shot_ids"] == ["s1"]
    assert out["text_artifacts"] == []
    assert out["ok"] is False


def test_model_shot_id_is_ignored_input_is_trusted(tmp_path, fake_frames):
    # vision が幻覚の shot_id を返しても、artifact は入力の shot_id (s1) に紐付ける。
    def _wrong_sid(prompt, paths, timeout_sec=600):
        data = [{"frame_index": i, "shot_id": "HALLUCINATED",
                 "has_text": (i == 0), "garbled": (i == 0), "note": "n"} for i in range(len(paths))]
        return {"ok": True, "data": data, "model_used": "m", "error": None}

    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"), _vision_fn=_wrong_sid,
    )
    assert [a["shot_id"] for a in out["text_artifacts"]] == ["s1"]


def test_vision_failure_marks_not_ok(tmp_path, fake_frames):
    def _fail(prompt, paths, timeout_sec=600):
        return {"ok": False, "error": "all_models_failed", "data": None}

    out = ctc.run_clip_text_check(
        [_clip("s1", str(tmp_path / "s1.mp4"))],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"), _vision_fn=_fail,
    )
    assert out["ok"] is False
    assert out["text_artifacts"] == []


def test_missing_clip_file_skipped(tmp_path, fake_frames):
    out = ctc.run_clip_text_check(
        [{"shot_id": "s1", "clip_path": str(tmp_path / "nope.mp4"), "duration_sec": 5}],
        ffmpeg_bin="ffmpeg", cfg={}, tmp_dir=str(tmp_path / "t"), _vision_fn=_vision_ok({}),
    )
    assert out["checked_shot_ids"] == []
    assert out["text_artifacts"] == []


def test_sample_times_default_three_frames_within_duration():
    times = ctc._sample_times_for_clip(6.0)
    assert len(times) == 3
    assert times[0] < times[1] < times[2]
    assert times[0] >= 0.0 and times[-1] <= 6.0


def test_sample_times_respects_trim_start_offset():
    times = ctc._sample_times_for_clip(4.0, start_sec=10.0, num_frames=3)
    assert len(times) == 3
    assert all(10.0 <= t <= 14.0 for t in times)


def test_sample_times_unknown_duration_head_only():
    assert ctc._sample_times_for_clip(None) == [round(ctc._HEAD_SAMPLE_SEC, 3)]
    assert ctc._sample_times_for_clip(0) == [round(ctc._HEAD_SAMPLE_SEC, 3)]


def test_sample_times_unknown_duration_with_start_offset():
    assert ctc._sample_times_for_clip(None, start_sec=5.0) == [round(5.0 + ctc._HEAD_SAMPLE_SEC, 3)]
