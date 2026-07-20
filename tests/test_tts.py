# -*- coding: utf-8 -*-
import urllib.error
from pathlib import Path

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


# --- synthesize_segments（音声主導タイミング同期モード用のショット単位合成ヘルパー） ---------


def test_synthesize_segments_success_measures_each_segment_duration(tmp_path):
    """SilentTTSBackend実体（実ffmpeg）で、文字数が異なる断片ごとに異なる実測尺が得られること。"""
    cfg = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg"), "ffprobe_bin": str(project_root() / "bin" / "ffprobe")}
    texts = ["あ" * 6, "あ" * 30]  # 6字/秒換算で概ね1秒 と 5秒
    result = tts.synthesize_segments(texts, tmp_path / "segments", cfg, voice="Kyoko")

    assert result["ok"] is True
    assert len(result["segments"]) == 2
    for seg in result["segments"]:
        assert Path(seg["path"]).exists()
        assert seg["duration_sec"] > 0
    # 長いテキストの方が実測尺が長い
    assert result["segments"][1]["duration_sec"] > result["segments"][0]["duration_sec"]
    assert result["backend"] is not None


def test_synthesize_segments_creates_out_dir_if_missing(tmp_path):
    cfg = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    out_dir = tmp_path / "nested" / "segments"
    assert not out_dir.exists()
    result = tts.synthesize_segments(["こんにちは"], out_dir, cfg, voice="Kyoko")
    assert result["ok"] is True
    assert out_dir.exists()


class _RaisesOnSecondCallBackend(tts.TTSBackend):
    """1断片目は成功、2断片目は例外で失敗するフェイクバックエンド（部分失敗の再現用）。"""

    name = "fake"

    def __init__(self, voice="Kyoko"):
        self.voice = voice
        self.calls = 0

    def synthesize(self, text, out_wav_path, cfg=None):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated segment TTS failure")
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake", "duration_sec": 1.5, "is_silent": False}


def test_synthesize_segments_fails_fast_when_one_segment_raises(tmp_path, monkeypatch):
    fake_backend = _RaisesOnSecondCallBackend()
    monkeypatch.setattr(tts, "get_tts_backend", lambda voice=None, cfg=None: fake_backend)

    result = tts.synthesize_segments(["ok", "boom", "never reached"], tmp_path / "segs", {}, voice="Kyoko")

    assert result["ok"] is False
    assert result["segments"] == []
    assert "segment_1_exception" in result["fallback_reason"]
    assert fake_backend.calls == 2  # 3断片目は試行されず即打ち切り


class _EmptyOutputBackend(tts.TTSBackend):
    name = "fake_empty"

    def synthesize(self, text, out_wav_path, cfg=None):
        # ファイルを作らない（合成失敗を模擬）
        return {"backend": "fake_empty", "duration_sec": 1.0, "is_silent": False}


def test_synthesize_segments_fails_when_output_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "get_tts_backend", lambda voice=None, cfg=None: _EmptyOutputBackend())
    result = tts.synthesize_segments(["text"], tmp_path / "segs", {}, voice="Kyoko")
    assert result["ok"] is False
    assert "empty_output" in result["fallback_reason"]


class _ZeroDurationBackend(tts.TTSBackend):
    name = "fake_zero"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"x")
        return {"backend": "fake_zero", "duration_sec": 0.0, "is_silent": False}


def test_synthesize_segments_fails_when_duration_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "get_tts_backend", lambda voice=None, cfg=None: _ZeroDurationBackend())
    result = tts.synthesize_segments(["text"], tmp_path / "segs", {}, voice="Kyoko")
    assert result["ok"] is False
    assert "invalid_duration" in result["fallback_reason"]


# --- prosody話速（UGC体験談トーン） ---


def _make_fixture_bytes(tmp_path):
    fixture_wav = tmp_path / "fixture.wav"
    cfg_for_fixture = {"ffmpeg_bin": str(project_root() / "bin" / "ffmpeg")}
    tts.SilentTTSBackend().synthesize("あ", str(fixture_wav), cfg_for_fixture)
    return fixture_wav.read_bytes()


def test_fish_audio_includes_prosody_speed_in_payload_by_default(tmp_path, monkeypatch):
    """fish_audio設定にprosody_speedが無い場合、既定1.15がbodyのprosody.speedに載ること。"""
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_bytes = _make_fixture_bytes(tmp_path)
    captured = {}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        captured["payload"] = payload
        return fixture_bytes

    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    backend.synthesize("こんにちは", str(tmp_path / "out.wav"), _FISH_CFG)

    assert captured["payload"]["prosody"] == {"speed": tts._FISH_AUDIO_DEFAULT_PROSODY_SPEED}


def test_fish_audio_prosody_speed_fixation_smoke(tmp_path, monkeypatch):
    """prosody形状の固定化スモーク（[中]バグ修正）: 実機検証済み(2026-07-20)の
    speed=1.15 が既定値としてpayloadに載ることを固定する。ここが変わると同文の音声尺が
    変わり、TTS駆動同期モードのショット尺・テロップ配置が全体的にずれる。
    """
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_bytes = _make_fixture_bytes(tmp_path)
    captured = {}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        captured["payload"] = payload
        return fixture_bytes

    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    backend.synthesize("同期モードの実効テスト用テキスト", str(tmp_path / "out.wav"), _FISH_CFG)

    # 固定: 既定prosody形状は {"speed": 1.15}（実APIで5.67秒→4.95秒を実測済み）。
    assert captured["payload"]["prosody"] == {"speed": 1.15}
    assert tts._FISH_AUDIO_DEFAULT_PROSODY_SPEED == 1.15


def test_fish_audio_uses_configured_prosody_speed(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_bytes = _make_fixture_bytes(tmp_path)
    captured = {}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        captured["payload"] = payload
        return fixture_bytes

    custom_cfg = {"tts": {"engine": "fish_audio", "fish_audio": {"prosody_speed": 1.3}}}
    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    backend.synthesize("こんにちは", str(tmp_path / "out.wav"), custom_cfg)

    assert captured["payload"]["prosody"] == {"speed": 1.3}


def test_fish_audio_omits_prosody_when_speed_is_1(tmp_path, monkeypatch):
    """prosody_speed==1.0（等倍）のときはbodyにprosodyキーを載せない（従来挙動を維持）。"""
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    fixture_bytes = _make_fixture_bytes(tmp_path)
    captured = {}

    def fake_post(url, payload, api_key, model, timeout_sec=None):
        captured["payload"] = payload
        return fixture_bytes

    custom_cfg = {"tts": {"engine": "fish_audio", "fish_audio": {"prosody_speed": 1.0}}}
    backend = tts.FishAudioTTSBackend(voice="Kyoko", _http_post=fake_post)
    backend.synthesize("こんにちは", str(tmp_path / "out.wav"), custom_cfg)

    assert "prosody" not in captured["payload"]


def test_say_backend_command_uses_configured_rate(tmp_path, monkeypatch):
    """sayフォールバックの読み上げ速度が定数 _SAY_RATE_WPM で `-r` 指定されること。"""
    run_calls = []

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/say")
    monkeypatch.setattr(tts, "_voice_available", lambda v: True)
    monkeypatch.setattr(tts, "_file_nonempty", lambda p: True)
    monkeypatch.setattr(tts, "_ffprobe_duration", lambda b, p: 2.0)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    backend = tts.SayTTSBackend(voice="Kyoko")
    cfg = {"ffmpeg_bin": "/x/ffmpeg", "ffprobe_bin": "/x/ffprobe"}
    meta = backend.synthesize("こんにちは", str(tmp_path / "out.wav"), cfg)

    assert meta["backend"] == "say"
    say_cmd = run_calls[0]
    assert "-r" in say_cmd
    assert str(tts._SAY_RATE_WPM) in say_cmd
    # -r の直後に速度値が来ていること
    assert say_cmd[say_cmd.index("-r") + 1] == str(tts._SAY_RATE_WPM)
