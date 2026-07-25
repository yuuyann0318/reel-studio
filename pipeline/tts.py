# -*- coding: utf-8 -*-
"""日本語ナレーションTTS。既定は macOS `say -v Kyoko`。IF化して将来差替え可能にする。

`say` コマンド or 指定voiceが使えない環境では、無音トラック（台本の文字数から
概算した尺）にフォールバックし、字幕主体で完成させる（TTSが無くてもパイプライン
全体が必ず完成することを優先する設計）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from pipeline.config import load_config, project_root

# 日本語の平均読み上げ速度の概算（文字/秒）。無音フォールバック時の尺見積りに使う。
_JP_CHARS_PER_SEC = 6.0
_MIN_FALLBACK_SEC = 2.0

# --- Fish Audio TTS 関連の既定値（pipeline/config.py の _DEFAULTS には未登録。統合フェーズで別途追加予定） ---
_FISH_AUDIO_URL = "https://api.fish.audio/v1/tts"
_FISH_AUDIO_DEFAULT_MODEL = "s2.1-pro-free"
_FISH_AUDIO_DEFAULT_FORMAT = "wav"
_FISH_AUDIO_DEFAULT_API_KEY_ENV = "FISH_AUDIO_API_KEY"
_FISH_AUDIO_RETRY_BACKOFF_SEC = [2, 5]
_FISH_AUDIO_MAX_RETRIES = 2
# UGC体験談トーンに合わせた既定の話速。1.0なら等倍（prosodyをbodyに載せない）。
_FISH_AUDIO_DEFAULT_PROSODY_SPEED = 1.15

# `say` フォールバック時の読み上げ速度（words/min）。UGCの早口体験談トーンに寄せる。
_SAY_RATE_WPM = 230


class TTSError(RuntimeError):
    pass


class TTSBackend:
    name = "base"

    def synthesize(self, text: str, out_wav_path: str, cfg: dict = None) -> dict:
        """textを読み上げ、out_wav_path (wav) に保存する。

        Returns: {"backend": str, "duration_sec": float, "is_silent": bool}
        """
        raise NotImplementedError


def _voice_available(voice_name):
    try:
        proc = subprocess.run(["say", "-v", "?"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception:
        return False
    listing = proc.stdout.decode("utf-8", "replace")
    return any(line.split()[0] == voice_name for line in listing.splitlines() if line.strip())


def _ffprobe_duration(ffprobe_bin, path):
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


class SayTTSBackend(TTSBackend):
    """macOS `say` コマンドによるTTS。"""

    name = "say"

    def __init__(self, voice="Kyoko"):
        self.voice = voice

    def synthesize(self, text: str, out_wav_path: str, cfg: dict = None) -> dict:
        cfg = cfg or load_config()
        ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        ffprobe_bin = cfg.get("ffprobe_bin") or str(project_root() / "bin" / "ffprobe")

        if shutil.which("say") is None or not _voice_available(self.voice):
            return self._silent_fallback(text, out_wav_path, cfg, "voice_unavailable")

        aiff_path = out_wav_path + ".say.aiff"
        try:
            proc = subprocess.run(
                ["say", "-v", self.voice, "-r", str(_SAY_RATE_WPM), "-o", aiff_path, text or ""],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            )
        except Exception as exc:
            raise TTSError("say コマンド実行に失敗しました: {}".format(exc))
        if proc.returncode != 0 or not _file_nonempty(aiff_path):
            return self._silent_fallback(text, out_wav_path, cfg, "say_failed")

        conv = subprocess.run(
            [ffmpeg_bin, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out_wav_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _safe_remove(aiff_path)
        if conv.returncode != 0 or not _file_nonempty(out_wav_path):
            return self._silent_fallback(text, out_wav_path, cfg, "ffmpeg_convert_failed")

        duration = _ffprobe_duration(ffprobe_bin, out_wav_path)
        return {
            "backend": self.name, "duration_sec": duration, "is_silent": False,
            "requested_backend": self.name, "fallback_reason": None,
        }

    def _silent_fallback(self, text, out_wav_path, cfg, reason):
        result = SilentTTSBackend().synthesize(text, out_wav_path, cfg)
        result["requested_backend"] = self.name
        result["fallback_reason"] = reason
        return result


class SilentTTSBackend(TTSBackend):
    """`say`/voiceが使えない環境向けフォールバック。台本文字数から尺を概算した無音wavを作る。"""

    name = "silent"

    def synthesize(self, text: str, out_wav_path: str, cfg: dict = None) -> dict:
        cfg = cfg or load_config()
        ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        char_count = len((text or "").strip())
        duration = max(_MIN_FALLBACK_SEC, char_count / _JP_CHARS_PER_SEC)
        cmd = [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t", "{:.3f}".format(duration),
            "-c:a", "pcm_s16le",
            out_wav_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise TTSError("無音フォールバックwav生成に失敗しました: {}".format(proc.stderr.decode("utf-8", "replace")[:300]))
        return {"backend": self.name, "duration_sec": duration, "is_silent": True}


def synthesize_silent_track(out_wav_path, duration_sec, cfg=None):
    """P0-2: narration_mode="absent"（参考にナレーション無し）用の無音トラック生成。

    指定尺ちょうどの無音 wav を作る（TTS はスキップし、BGM+テロップだけで成立させる）。
    render 段の音声ミックス経路は narration トラックの存在を前提にしているため、
    無音でも「尺ぴったりの音声トラック」を渡すことで BGM が主音声になり、
    ducking も鳴らない（無音なので）。動画尺は参考のカット割り通り＝duration_drift 0。
    """
    cfg = cfg or load_config()
    ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
    try:
        dur = float(duration_sec)
    except (TypeError, ValueError):
        dur = 0.0
    dur = max(_MIN_FALLBACK_SEC, dur)
    cmd = [
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
        "-t", "{:.3f}".format(dur),
        "-c:a", "pcm_s16le",
        str(out_wav_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise TTSError(
            "無音ナレーショントラック生成に失敗しました: {}".format(
                proc.stderr.decode("utf-8", "replace")[:300]
            )
        )
    return {"backend": "none", "duration_sec": dur, "is_silent": True, "mode": "none"}


def _default_fish_audio_http_post(url, payload, api_key, model, timeout_sec=60):
    """Fish Audio TTS APIへPOSTし、音声バイト列を返す(stdlib urllib.requestのみ・依存追加ゼロ)。

    HTTPエラーは urllib.error.HTTPError（exc.codeで判別）、接続/タイムアウト等は
    OSError（URLErrorのスーパークラス）としてそのまま呼び出し元に伝播させる。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer {}".format(api_key))
    req.add_header("model", model)
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=timeout_sec)
    try:
        return resp.read()
    finally:
        resp.close()


class FishAudioTTSBackend(TTSBackend):
    """Fish Audio TTS API によるナレーション音声合成。

    APIキー未設定・認証エラー・リトライ全滅・保存/変換失敗のいずれでも例外を出さず、
    `SayTTSBackend`（さらにその内部で `SilentTTSBackend`）へ即フォールバックする。
    パイプライン全体がTTSの都合で止まらないことを最優先する既存設計を踏襲する。
    """

    name = "fish_audio"

    def __init__(self, voice="Kyoko", _http_post=None, _sleep=None):
        self.voice = voice
        self._http_post = _http_post or _default_fish_audio_http_post
        self._sleep = _sleep or time.sleep

    def _fallback_to_say(self, text, out_wav_path, cfg, fallback_reason):
        result = SayTTSBackend(voice=self.voice).synthesize(text, out_wav_path, cfg)
        result["requested_backend"] = self.name
        result["fallback_reason"] = fallback_reason
        return result

    def synthesize(self, text: str, out_wav_path: str, cfg: dict = None) -> dict:
        cfg = cfg or load_config()
        fish_cfg = (cfg.get("tts") or {}).get("fish_audio") or {}
        api_key_env = fish_cfg.get("api_key_env") or _FISH_AUDIO_DEFAULT_API_KEY_ENV
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return self._fallback_to_say(text, out_wav_path, cfg, "api_key_missing")

        model = fish_cfg.get("model") or _FISH_AUDIO_DEFAULT_MODEL
        audio_format = fish_cfg.get("format") or _FISH_AUDIO_DEFAULT_FORMAT
        reference_id = fish_cfg.get("reference_id")
        # デッドコンフィグ対策: fish_cfg["timeout_sec"](config.json product_images... ではなく
        # tts.fish_audio.timeout_sec)を実際のHTTPタイムアウトへ配線する。従来はこの値を読まず、
        # _default_fish_audio_http_post の既定値(60秒)が常に使われていた。
        http_timeout_sec = fish_cfg.get("timeout_sec", 60)

        payload = {"text": text or "", "format": audio_format}
        if reference_id:
            payload["reference_id"] = reference_id
        # UGC体験談トーンに寄せた話速。等倍(1.0)ならbodyに載せない（従来挙動を維持）。
        # 実機検証済み(2026-07-20): 同一文でspeed=1.15が5.67秒→4.95秒に短縮されることを実APIで確認。
        prosody_speed = fish_cfg.get("prosody_speed", _FISH_AUDIO_DEFAULT_PROSODY_SPEED)
        if prosody_speed and prosody_speed != 1.0:
            payload["prosody"] = {"speed": prosody_speed}

        audio_bytes = None
        fallback_reason = None
        attempt = 0
        while True:
            try:
                audio_bytes = self._http_post(_FISH_AUDIO_URL, payload, api_key, model, timeout_sec=http_timeout_sec)
                break
            except urllib.error.HTTPError as exc:
                code = exc.code
                if code == 429 or code >= 500:
                    if attempt >= _FISH_AUDIO_MAX_RETRIES:
                        fallback_reason = "retry_exhausted"
                        break
                    self._sleep(_FISH_AUDIO_RETRY_BACKOFF_SEC[min(attempt, len(_FISH_AUDIO_RETRY_BACKOFF_SEC) - 1)])
                    attempt += 1
                    continue
                # 401/403/400 等はリトライせず即フォールバック
                fallback_reason = "http_error_{}".format(code)
                break
            except OSError as exc:
                # URLError/timeout等の接続系エラーのみリトライ対象
                if attempt >= _FISH_AUDIO_MAX_RETRIES:
                    fallback_reason = "retry_exhausted"
                    break
                self._sleep(_FISH_AUDIO_RETRY_BACKOFF_SEC[min(attempt, len(_FISH_AUDIO_RETRY_BACKOFF_SEC) - 1)])
                attempt += 1
                continue
            except Exception:
                fallback_reason = "unknown_error"
                break

        if audio_bytes is None:
            return self._fallback_to_say(text, out_wav_path, cfg, fallback_reason or "unknown_error")

        ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        ffprobe_bin = cfg.get("ffprobe_bin") or str(project_root() / "bin" / "ffprobe")
        tmp_path = out_wav_path + ".fish.{}".format(audio_format)
        try:
            with open(tmp_path, "wb") as f:
                f.write(audio_bytes)
        except OSError:
            return self._fallback_to_say(text, out_wav_path, cfg, "save_failed")

        conv = subprocess.run(
            [ffmpeg_bin, "-y", "-i", tmp_path, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out_wav_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _safe_remove(tmp_path)
        if conv.returncode != 0 or not _file_nonempty(out_wav_path):
            return self._fallback_to_say(text, out_wav_path, cfg, "ffmpeg_convert_failed")

        duration = _ffprobe_duration(ffprobe_bin, out_wav_path)
        return {
            "backend": self.name, "duration_sec": duration, "is_silent": False,
            "requested_backend": self.name, "fallback_reason": None,
        }


def _file_nonempty(path):
    import os
    return os.path.exists(path) and os.path.getsize(path) > 0


def _safe_remove(path):
    import os
    try:
        os.remove(path)
    except OSError:
        pass


def get_tts_backend(voice="Kyoko", cfg=None):
    """TTSバックエンドを選択する。

    cfg["tts"]["engine"] == "fish_audio" のときのみ FishAudioTTSBackend
    （内部にsay/silentフォールバックを内蔵）を返す。cfg未指定・それ以外は
    従来どおり SayTTSBackend を返す（後方互換）。
    """
    if cfg is not None and (cfg.get("tts") or {}).get("engine") == "fish_audio":
        return FishAudioTTSBackend(voice=voice)
    return SayTTSBackend(voice=voice)


def synthesize_segments(texts, out_dir, cfg=None, voice="Kyoko"):
    """ナレーションをショット単位の断片ごとに合成する（音声主導タイミング同期モード用）。

    get_tts_backend()が選ぶバックエンド（fish_audio -> say -> silent の既存フォールバック
    チェーンをそのまま内包）で、texts の各要素を1断片ずつ合成し、ffprobe実測の尺を
    そのまま採用する（=テロップ/ショット尺の同期はこの実測値を基準に呼び出し側が組み立てる）。

    1断片でも合成に失敗（例外・空ファイル・尺0以下）したら即座に ok=False で打ち切る
    （呼び出し側は全文方式へフォールバックすること。パイプライン全体が必ず完成することを
    優先する既存TTS設計を踏襲し、部分的に同期済み・一部旧方式という中途半端な状態は作らない）。

    Args:
        texts: ショット順のナレーション断片文字列のリスト。
        out_dir: 断片wavの保存先ディレクトリ（無ければ作成する）。
        cfg: config dict（Noneならload_config()）。
        voice: get_tts_backend()へ渡す音声名。

    Returns:
        {"ok": bool, "segments": [{"path": str, "duration_sec": float}, ...],
         "backend": str|None, "fallback_reason": str|None}
        segments は成功した断片のみ（ok=Falseのときは空リスト）。
    """
    cfg = cfg or load_config()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = get_tts_backend(voice=voice, cfg=cfg)

    segments = []
    used_backend = None
    fallback_reason = None
    for i, text in enumerate(texts or []):
        out_path = out_dir / "seg_{:03d}.wav".format(i)
        try:
            meta = backend.synthesize(text or "", str(out_path), cfg)
        except Exception as exc:
            return {
                "ok": False, "segments": [], "backend": None,
                "fallback_reason": "segment_{}_exception:{}".format(i, exc),
            }
        if not _file_nonempty(str(out_path)):
            return {
                "ok": False, "segments": [], "backend": None,
                "fallback_reason": "segment_{}_empty_output".format(i),
            }
        duration = meta.get("duration_sec")
        if not duration or duration <= 0:
            return {
                "ok": False, "segments": [], "backend": None,
                "fallback_reason": "segment_{}_invalid_duration".format(i),
            }
        segments.append({"path": str(out_path), "duration_sec": float(duration)})
        used_backend = meta.get("backend") or used_backend
        if meta.get("fallback_reason"):
            fallback_reason = meta.get("fallback_reason")

    return {"ok": True, "segments": segments, "backend": used_backend, "fallback_reason": fallback_reason}
