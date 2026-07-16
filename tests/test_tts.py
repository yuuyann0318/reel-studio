# -*- coding: utf-8 -*-
from pipeline import tts
from pipeline.config import project_root


def test_silent_backend_generates_wav_of_expected_length(tmp_path):
    cfg = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    out_path = tmp_path / "silent.wav"
    backend = tts.SilentTTSBackend()
    meta = backend.synthesize("あ" * 60, str(out_path), cfg)  # 60字 / 6字/秒 = 10秒
    assert meta["is_silent"] is True
    assert out_path.exists()
    assert abs(meta["duration_sec"] - 10.0) < 0.5


def test_silent_backend_respects_minimum_duration(tmp_path):
    cfg = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    out_path = tmp_path / "silent_short.wav"
    backend = tts.SilentTTSBackend()
    meta = backend.synthesize("短", str(out_path), cfg)
    assert meta["duration_sec"] >= tts._MIN_FALLBACK_SEC


def test_get_tts_backend_returns_say_backend():
    backend = tts.get_tts_backend(voice="Kyoko")
    assert isinstance(backend, tts.SayTTSBackend)
    assert backend.voice == "Kyoko"
