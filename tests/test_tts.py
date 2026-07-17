# -*- coding: utf-8 -*-
import urllib.error

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


# --- FishAudioTTSBackend ---

_FISH_CFG = {"tts": {"engine": "fish_audio", "fish_audio": {}}}


class _FakeSayBackend(tts.TTSBackend):
    """SayTTSBackendの代替。実say/ffmpegを叩かず即座に決定論的な結果を返す。"""

    name = "say"
    calls = []

    def __init__(self, voice="Kyoko"):
        self.voice = voice

    def synthesize(self, text, out_wav_path, cfg=None):
        _FakeSayBackend.calls.append((text, out_wav_path))
        return {"backend": "say", "duration_sec": 3.0, "is_silent": False}


def _install_fake_say(monkeypatch):
    _FakeSayBackend.calls = []
    monkeypatch.setattr(tts, "SayTTSBackend", _FakeSayBackend)
    return _FakeSayBackend


def test_fish_audio_success_converts_and_measures_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    # 実ffmpegで生成した既知尺(2.0秒)の無音wavを「Fish Audioが返した音声バイト列」に見立てる。
    fixture_wav = tmp_path / "fixture.wav"
    cfg_for_fixture = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    fixture_meta = tts.SilentTTSBackend().synthesize("あ" * 12, str(fixture_wav), cfg_for_fixture)
    assert fixture_meta["duration_sec"] >= tts._MIN_FALLBACK_SEC
    fixture_bytes = fixture_wav.read_bytes()

    calls = []

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        calls.append((url, payload, api_key, model, timeout_sec))
        return fixture_bytes

    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    out_path = tmp_path / "out.wav"
    meta = backend.synthesize("あ" * 12, str(out_path), _FISH_CFG)

    assert len(calls) == 1
    assert calls[0][0] == tts._FISH_AUDIO_URL
    assert calls[0][2] == "dummy-key"
    assert meta["backend"] == "fish_audio"
    assert meta["requested_backend"] == "fish_audio"
    assert meta["fallback_reason"] is None
    assert meta["is_silent"] is False
    assert out_path.exists()
    assert abs(meta["duration_sec"] - fixture_meta["duration_sec"]) < 0.5


def test_fish_audio_defaults_timeout_sec_to_60_when_not_configured(tmp_path, monkeypatch):
    """デッドコンフィグ回帰: fish_cfgにtimeout_secが無い場合は既定値60秒がHTTP呼び出しへ渡ること。"""
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_wav = tmp_path / "fixture.wav"
    cfg_for_fixture = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    tts.SilentTTSBackend().synthesize("あ", str(fixture_wav), cfg_for_fixture)
    fixture_bytes = fixture_wav.read_bytes()

    calls = []

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        calls.append(timeout_sec)
        return fixture_bytes

    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    out_path = tmp_path / "out.wav"
    backend.synthesize("こんにちは", str(out_path), _FISH_CFG)

    assert calls == [60]


def test_fish_audio_passes_cfg_timeout_sec_to_http_post(tmp_path, monkeypatch):
    """デッドコンフィグ回帰: cfg["tts"]["fish_audio"]["timeout_sec"] が実際のHTTPタイムアウトへ
    配線されること（従来はこの値が読まれず、_default_fish_audio_http_post の既定値60秒が
    常に使われていた）。"""
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_wav = tmp_path / "fixture.wav"
    cfg_for_fixture = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    tts.SilentTTSBackend().synthesize("あ", str(fixture_wav), cfg_for_fixture)
    fixture_bytes = fixture_wav.read_bytes()

    calls = []

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        calls.append(timeout_sec)
        return fixture_bytes

    custom_cfg = {"tts": {"engine": "fish_audio", "fish_audio": {"timeout_sec": 5}}}
    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    out_path = tmp_path / "out.wav"
    backend.synthesize("こんにちは", str(out_path), custom_cfg)

    assert calls == [5]


def test_fish_audio_falls_back_to_say_when_api_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    _install_fake_say(monkeypatch)

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        raise AssertionError("APIキー未設定時はHTTP呼び出し自体が発生してはいけない")

    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    out_path = tmp_path / "out.wav"
    meta = backend.synthesize("こんにちは", str(out_path), _FISH_CFG)

    assert meta["backend"] == "say"
    assert meta["requested_backend"] == "fish_audio"
    assert meta["fallback_reason"] == "api_key_missing"
    assert len(_FakeSayBackend.calls) == 1


def test_fish_audio_retries_on_5xx_then_falls_back_after_exhausting_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    _install_fake_say(monkeypatch)

    call_count = {"n": 0}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        call_count["n"] += 1
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)

    sleep_calls = []

    backend = tts.FishAudioTTSBackend(
        voice="Kyoko", _http_post=fake_post, _sleep=lambda sec: sleep_calls.append(sec)
    )
    out_path = tmp_path / "out.wav"
    meta = backend.synthesize("こんにちは", str(out_path), _FISH_CFG)

    assert call_count["n"] == 1 + tts._FISH_AUDIO_MAX_RETRIES  # 初回 + リトライ2回
    assert sleep_calls == tts._FISH_AUDIO_RETRY_BACKOFF_SEC
    assert meta["backend"] == "say"
    assert meta["requested_backend"] == "fish_audio"
    assert meta["fallback_reason"] == "retry_exhausted"
    assert len(_FakeSayBackend.calls) == 1


def test_fish_audio_401_falls_back_immediately_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    _install_fake_say(monkeypatch)

    call_count = {"n": 0}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        call_count["n"] += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    sleep_calls = []

    backend = tts.FishAudioTTSBackend(
        voice="Kyoko", _http_post=fake_post, _sleep=lambda sec: sleep_calls.append(sec)
    )
    out_path = tmp_path / "out.wav"
    meta = backend.synthesize("こんにちは", str(out_path), _FISH_CFG)

    assert call_count["n"] == 1  # リトライしない
    assert sleep_calls == []
    assert meta["backend"] == "say"
    assert meta["requested_backend"] == "fish_audio"
    assert meta["fallback_reason"] == "http_error_401"


def test_get_tts_backend_backward_compatible_without_cfg():
    """cfg未指定・cfgにtts設定が無い・engineがfish_audio以外はいずれも従来どおりSayTTSBackend。"""
    assert isinstance(tts.get_tts_backend(voice="Kyoko"), tts.SayTTSBackend)
    assert isinstance(tts.get_tts_backend(voice="Kyoko", cfg={}), tts.SayTTSBackend)
    assert isinstance(
        tts.get_tts_backend(voice="Kyoko", cfg={"tts": {"engine": "say"}}), tts.SayTTSBackend
    )


def test_get_tts_backend_returns_fish_audio_backend_when_configured():
    backend = tts.get_tts_backend(voice="Kyoko", cfg=_FISH_CFG)
    assert isinstance(backend, tts.FishAudioTTSBackend)
    assert backend.voice == "Kyoko"
