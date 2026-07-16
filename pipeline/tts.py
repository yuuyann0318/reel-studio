# -*- coding: utf-8 -*-
"""日本語ナレーションTTS。既定は macOS `say -v Kyoko`。IF化して将来差替え可能にする。

`say` コマンド or 指定voiceが使えない環境では、無音トラック（台本の文字数から
概算した尺）にフォールバックし、字幕主体で完成させる（TTSが無くてもパイプライン
全体が必ず完成することを優先する設計）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import shutil
import subprocess

from pipeline.config import load_config, project_root

# 日本語の平均読み上げ速度の概算（文字/秒）。無音フォールバック時の尺見積りに使う。
_JP_CHARS_PER_SEC = 6.0
_MIN_FALLBACK_SEC = 2.0


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
            return SilentTTSBackend().synthesize(text, out_wav_path, cfg)

        aiff_path = out_wav_path + ".say.aiff"
        try:
            proc = subprocess.run(
                ["say", "-v", self.voice, "-o", aiff_path, text or ""],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            )
        except Exception as exc:
            raise TTSError("say コマンド実行に失敗しました: {}".format(exc))
        if proc.returncode != 0 or not _file_nonempty(aiff_path):
            return SilentTTSBackend().synthesize(text, out_wav_path, cfg)

        conv = subprocess.run(
            [ffmpeg_bin, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out_wav_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _safe_remove(aiff_path)
        if conv.returncode != 0 or not _file_nonempty(out_wav_path):
            return SilentTTSBackend().synthesize(text, out_wav_path, cfg)

        duration = _ffprobe_duration(ffprobe_bin, out_wav_path)
        return {"backend": self.name, "duration_sec": duration, "is_silent": False}


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


def _file_nonempty(path):
    import os
    return os.path.exists(path) and os.path.getsize(path) > 0


def _safe_remove(path):
    import os
    try:
        os.remove(path)
    except OSError:
        pass


def get_tts_backend(voice="Kyoko"):
    return SayTTSBackend(voice=voice)
