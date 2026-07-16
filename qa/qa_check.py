# -*- coding: utf-8 -*-
"""出力自動検証。`bin/ffprobe`/`bin/ffmpeg` の実測値で合否をJSONで返す。

video-auto-editor/qa/qa_check.py のパターン（「実測値を取る関数」measure_*/probe_* と
「判定する純関数」judge_* を分離し、judge_* だけをユニットテスト対象にする）を踏襲。

検査項目（KR7準拠）:
  - 解像度 == 1080x1920
  - 尺が目標 ±15%
  - 音声ストリーム有
  - 平均輝度で全黒でない（blackdetectで黒フレーム検出ゼロ）
  - ファイルサイズ > 0

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import os
import re
import subprocess

_BLACK_START_RE = re.compile(r"black_start")


# ---------------------------------------------------------------------------
# 判定（純関数・ユニットテスト対象）
# ---------------------------------------------------------------------------

def judge_resolution(probe_data, expected_w=1080, expected_h=1920):
    probe_data = probe_data or {}
    w, h = probe_data.get("width"), probe_data.get("height")
    ok = (w == expected_w and h == expected_h)
    errors = [] if ok else ["解像度不一致: expected {}x{}, got {}x{}".format(expected_w, expected_h, w, h)]
    return ok, errors


def judge_duration(output_duration, target_duration_sec, tolerance_ratio=0.15):
    """尺が目標±tolerance_ratio(既定15%)に収まっているか。"""
    if target_duration_sec is None or target_duration_sec <= 0:
        return True, []
    tolerance = target_duration_sec * tolerance_ratio
    diff = abs((output_duration or 0.0) - target_duration_sec)
    ok = diff <= tolerance
    errors = [] if ok else [
        "尺不一致: 出力{:.2f}s, 目標{:.2f}s ±{:.0f}%({:.2f}s), 差{:.2f}s".format(
            output_duration or 0.0, target_duration_sec, tolerance_ratio * 100, tolerance, diff
        )
    ]
    return ok, errors


def judge_has_audio(probe_data):
    probe_data = probe_data or {}
    ok = bool(probe_data.get("has_audio"))
    errors = [] if ok else ["音声ストリームがありません"]
    return ok, errors


def judge_not_all_black(black_frame_count):
    ok = (black_frame_count or 0) == 0
    errors = [] if ok else ["黒フレーム検出: {}件（全黒/ほぼ全黒の疑い）".format(black_frame_count)]
    return ok, errors


def judge_file_size(size_bytes):
    ok = (size_bytes or 0) > 0
    errors = [] if ok else ["ファイルサイズが0バイトです"]
    return ok, errors


# ---------------------------------------------------------------------------
# 実測値取得（ffprobe/ffmpeg実行・外部バイナリ必須・ユニットテスト対象外）
# ---------------------------------------------------------------------------

def probe_output_spec(ffprobe_bin, video_path):  # pragma: no cover - 実機ffprobe必須
    cmd = [ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-print_format", "json", video_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout.decode("utf-8", "replace")


def measure_loudness(ffmpeg_bin, video_path):  # pragma: no cover - 実機ffmpeg必須
    cmd = [ffmpeg_bin, "-i", video_path, "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stderr.decode("utf-8", "replace")


def detect_black_frames(ffmpeg_bin, video_path, d=0.5, pic_th=0.98):  # pragma: no cover - 実機ffmpeg必須
    cmd = [ffmpeg_bin, "-i", video_path, "-vf", "blackdetect=d={}:pic_th={}".format(d, pic_th), "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stderr.decode("utf-8", "replace")


def _count_black_frames(stderr_text):
    return len(_BLACK_START_RE.findall(stderr_text or ""))


def _build_probe_data(ffprobe_json_data):
    fmt = ffprobe_json_data.get("format") or {}
    streams = ffprobe_json_data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
    a = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}
    return {
        "width": v.get("width"),
        "height": v.get("height"),
        "video_codec": v.get("codec_name"),
        "pix_fmt": v.get("pix_fmt"),
        "has_audio": bool(a),
        "audio_codec": a.get("codec_name"),
        "duration": float(fmt.get("duration") or v.get("duration") or 0.0),
    }


def run_qa(ffmpeg_bin, ffprobe_bin, output_path, target_duration_sec=None):
    """出力mp4に対する一連の検査を実行し、reportを返す（例外は投げない・呼び出し側でtry/exceptする前提）。"""
    items = {}

    raw = probe_output_spec(ffprobe_bin, output_path)
    try:
        ffprobe_json = json.loads(raw)
    except Exception:
        ffprobe_json = {}
    probe_data = _build_probe_data(ffprobe_json)

    ok, errs = judge_resolution(probe_data)
    items["resolution"] = {"ok": ok, "errors": errs, "measured": {"width": probe_data.get("width"), "height": probe_data.get("height")}}

    ok, errs = judge_duration(probe_data.get("duration"), target_duration_sec)
    items["duration"] = {"ok": ok, "errors": errs, "measured_duration_sec": probe_data.get("duration"), "target_duration_sec": target_duration_sec}

    ok, errs = judge_has_audio(probe_data)
    items["has_audio"] = {"ok": ok, "errors": errs, "measured": {"has_audio": probe_data.get("has_audio"), "audio_codec": probe_data.get("audio_codec")}}

    black_stderr = detect_black_frames(ffmpeg_bin, output_path)
    black_count = _count_black_frames(black_stderr)
    ok, errs = judge_not_all_black(black_count)
    items["not_all_black"] = {"ok": ok, "errors": errs, "black_frame_count": black_count}

    size_bytes = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    ok, errs = judge_file_size(size_bytes)
    items["file_size"] = {"ok": ok, "errors": errs, "size_bytes": size_bytes}

    overall_ok = all(v["ok"] for v in items.values())
    return {"overall_ok": overall_ok, "items": items, "probe_data": probe_data}


def main(argv=None):  # pragma: no cover - CLIエントリ
    import argparse
    from pipeline.config import load_config

    parser = argparse.ArgumentParser(description="higgsfield-auto-reel QAチェックCLI")
    parser.add_argument("video", help="検査対象mp4")
    parser.add_argument("--target-duration", type=float, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    report = run_qa(cfg["ffmpeg_bin"], cfg["ffprobe_bin"], args.video, target_duration_sec=args.target_duration)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["overall_ok"] else 2


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
