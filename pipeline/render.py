# -*- coding: utf-8 -*-
"""ffmpeg レンダリングコマンド構築（配列で返す）と実行分離。

video-auto-editor/pipeline/render.py のパターン（コマンド構築(build_*)は文字列配列を
返すだけの純関数・実行(run_ffmpeg)は別関数、9:16化・ASS焼込・BGMダッキング・
loudnorm 2パスの考え方）を踏襲。reel向けの違い:
  - 「元動画を切り出す」のではなく「ショットごとに生成済みのクリップを正規化して連結」
  - 音声トラックは各クリップに無く（mock/higgsfieldとも映像のみ生成）、ナレーションwavを
    別入力として合成する（[0:v]=連結映像, [1:a]=ナレーション, [2:a]=BGM(任意)）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import subprocess


def escape_ffmpeg_filter_path(path):
    """ffmpegフィルタグラフのオプション値としてパスを安全に埋め込む（シェルエスケープではない）。"""
    escaped = path.replace("\\", "\\\\").replace("'", "'\\''")
    return "'{}'".format(escaped)


def build_concat_list_content(clip_paths):
    """concat demuxer用 list.txt の中身を生成する。各行 `file '<path>'`。"""
    lines = []
    for p in clip_paths:
        escaped = p.replace("\\", "\\\\").replace("'", "'\\''")
        lines.append("file '{}'".format(escaped))
    return "\n".join(lines) + "\n"


def build_normalize_clip_cmd(ffmpeg_bin, in_path, out_path, duration_sec=None):
    """1ショットクリップを 1080x1920/30fps/yuv420p/音声なし へ正規化するコマンドを構築する。

    ビジュアルバックエンド（mock/higgsfield/cloudapi）が返すクリップの解像度・fpsが
    バックエンドによってまちまちでも、この正規化を経由すればconcat demuxerで安全に
    単純結合できる。duration_sec を指定すると出力尺を明示的に打ち切る
    （バックエンドが指定尺よりわずかに長い/短いクリップを返した場合の吸収）。
    """
    filt = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"
    cmd = [ffmpeg_bin, "-y", "-i", in_path, "-vf", filt, "-an"]
    if duration_sec:
        cmd += ["-t", "{:.3f}".format(duration_sec)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", out_path]
    return cmd


def build_concat_cmd(ffmpeg_bin, list_path, output_path):
    """concat demuxerでクリップ群を単純結合する（再エンコードなし）。"""
    return [ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]


def build_loudnorm_filter(measured=None, tp_target=-1.0):
    """loudnorm 2パス用フィルタ文字列を構築する。measured=None なら1パス目(計測のみ)。"""
    if measured:
        return (
            "loudnorm=I=-14:TP={tp}:LRA=11:"
            "measured_I={measured_I}:measured_TP={measured_TP}:measured_LRA={measured_LRA}:"
            "measured_thresh={measured_thresh}:offset={offset}:linear=true:print_format=json"
        ).format(tp=tp_target, **measured)
    return "loudnorm=I=-14:TP={tp}:LRA=11:print_format=json".format(tp=tp_target)


def build_final_cmd(ffmpeg_bin, concat_video_path, narration_wav_path, output_path, ass_path, fonts_dir,
                     bgm_path=None, out_duration=None, loudnorm_measured=None,
                     audio_prefilter=None, tp_target=-1.0):
    """字幕焼き込み＋ナレーション＋BGMダッキング＋loudnorm＋最終エンコード。

    入力: [0]=連結済み映像(音声なし), [1]=ナレーションwav, [2]=BGM(任意)。
    bgm_path=None ならBGM工程を丸ごとスキップ（BGM無しでも必ず完成させる）。
    out_duration は必須（ナレーションが映像より短い場合に無音で末尾を埋め、
    映像尺と音声尺を必ず一致させるため apad=whole_dur に使う）。
    """
    subtitles_filter = "subtitles=filename={}:fontsdir={}".format(
        escape_ffmpeg_filter_path(ass_path), escape_ffmpeg_filter_path(fonts_dir)
    )
    loudnorm_filter = build_loudnorm_filter(loudnorm_measured, tp_target=tp_target)
    prefilter = (audio_prefilter + ",") if audio_prefilter else ""
    dur = out_duration if out_duration else 0.0

    cmd = [ffmpeg_bin, "-y", "-i", concat_video_path, "-i", narration_wav_path]

    if bgm_path:
        cmd += ["-i", bgm_path]
        fade_st = max(dur - 2, 0.0)
        filter_complex = (
            "[0:v]{subs}[vout];"
            "[2:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},afade=t=out:st={fade_st:.3f}:d=2,volume=0.55[bgm];"
            "[1:a]{pre}apad=whole_dur={dur:.3f},asplit[voice][sc];"
            "[bgm][sc]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=500:makeup=1[duck];"
            "[voice][duck]amix=inputs=2:duration=longest:normalize=0[premix];"
            "[premix]{loud}[aout]"
        ).format(subs=subtitles_filter, dur=dur, fade_st=fade_st, pre=prefilter, loud=loudnorm_filter)
    else:
        filter_complex = "[0:v]{subs}[vout];[1:a]{pre}apad=whole_dur={dur:.3f},{loud}[aout]".format(
            subs=subtitles_filter, dur=dur, pre=prefilter, loud=loudnorm_filter
        )

    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", "[aout]"]

    if out_duration:
        cmd += ["-t", "{:.3f}".format(out_duration)]

    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
        "-crf", "18", "-preset", "slow",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def run_ffmpeg(cmd, timeout_sec=None):
    """build_*_cmd が返したコマンド配列を実行する。shell=False。"""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=timeout_sec,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }
