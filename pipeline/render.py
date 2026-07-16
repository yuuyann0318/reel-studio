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


def build_normalize_clip_cmd(ffmpeg_bin, in_path, out_path, duration_sec=None, trim_start=None):
    """1ショットクリップを 1080x1920/30fps/yuv420p/音声なし へ正規化するコマンドを構築する。

    ビジュアルバックエンド（mock/higgsfield/cloudapi）が返すクリップの解像度・fpsが
    バックエンドによってまちまちでも、この正規化を経由すればconcat demuxerで安全に
    単純結合できる。duration_sec を指定すると出力尺を明示的に打ち切る
    （バックエンドが指定尺よりわずかに長い/短いクリップを返した場合の吸収）。

    trim_start を指定すると、そこを起点にトリム（Studioのショット編集: trim.start〜trim.end）
    する。ffmpeg の `-ss`（`-i`の前）は高速シークだが、フレーム精度はキーフレーム間隔に
    依存する。本プロジェクトのクリップは短尺（数秒）のmock/higgsfield生成物のため実用上
    十分な精度で足りる。duration_sec には trim後の長さ（end-start）を渡すこと。
    trim_start=None（従来どおり）なら挙動を一切変えない（後方互換）。
    """
    filt = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"
    cmd = [ffmpeg_bin, "-y"]
    if trim_start:
        cmd += ["-ss", "{:.3f}".format(trim_start)]
    cmd += ["-i", in_path, "-vf", filt, "-an"]
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


def gain_db_to_linear(gain_db):
    """dB(電力比ではなく振幅比) を ffmpeg volume フィルタ用の線形係数へ変換する。"""
    return 10.0 ** (float(gain_db) / 20.0)


def build_sfx_overlay_filters(sfx, start_index):
    """SFXオーバーレイ用のフィルタ断片群と、amixでpremixと合流させるラベル配列を構築する（純関数・テスト対象）。

    sfx: [{"path","at_sec","gain_db"}, ...]（呼び出し側が-iで各pathを追加し、
    その入力インデックスがstart_indexから連番で振られている前提）。
    Returns: (filter_parts:list[str], mix_labels:list[str])
    """
    filter_parts = []
    mix_labels = []
    for i, s in enumerate(sfx or []):
        idx = start_index + i
        gain_db = s.get("gain_db", 0.0) or 0.0
        at_sec = max(0.0, float(s.get("at_sec", 0.0) or 0.0))
        delay_ms = int(round(at_sec * 1000))
        label = "sfx{}".format(i)
        filter_parts.append(
            "[{idx}:a]volume={gain:.4f},adelay={delay}|{delay}[{label}]".format(
                idx=idx, gain=gain_db_to_linear(gain_db), delay=delay_ms, label=label
            )
        )
        mix_labels.append("[{}]".format(label))
    return filter_parts, mix_labels


def build_final_cmd(ffmpeg_bin, concat_video_path, narration_wav_path, output_path, ass_path, fonts_dir,
                     bgm_path=None, out_duration=None, loudnorm_measured=None,
                     audio_prefilter=None, tp_target=-1.0, bgm_gain_db=None, sfx=None, ducking=True):
    """字幕焼き込み＋ナレーション＋BGMダッキング＋SFXオーバーレイ＋loudnorm＋最終エンコード。

    入力: [0]=連結済み映像(音声なし), [1]=ナレーションwav, [2]=BGM(任意),
    続く入力=sfx(任意・複数可)。
    bgm_path=None ならBGM工程を丸ごとスキップ（BGM無しでも必ず完成させる）。
    out_duration は必須（ナレーションが映像より短い場合に無音で末尾を埋め、
    映像尺と音声尺を必ず一致させるため apad=whole_dur に使う）。
    bgm_gain_db: BGM音量(dB)。Noneなら従来どおりvolume=0.55相当（後方互換）。
    ducking: Trueなら従来どおりナレーション区間でBGMをsidechaincompressで抑える。
    Falseならダッキングせず単純にamixする（plan.bgm.ducking=falseを反映するため）。
    sfx: [{"path","at_sec","gain_db"}, ...]。各SFXをat_secの位置にamixでオーバーレイする
    （出力尺は-tで最終的に打ち切るため変わらない）。
    """
    subtitles_filter = "subtitles=filename={}:fontsdir={}".format(
        escape_ffmpeg_filter_path(ass_path), escape_ffmpeg_filter_path(fonts_dir)
    )
    loudnorm_filter = build_loudnorm_filter(loudnorm_measured, tp_target=tp_target)
    prefilter = (audio_prefilter + ",") if audio_prefilter else ""
    dur = out_duration if out_duration else 0.0
    bgm_volume = gain_db_to_linear(bgm_gain_db) if bgm_gain_db is not None else 0.55

    cmd = [ffmpeg_bin, "-y", "-i", concat_video_path, "-i", narration_wav_path]
    next_index = 2

    if bgm_path:
        cmd += ["-i", bgm_path]
        bgm_index = next_index
        next_index += 1
        fade_st = max(dur - 2, 0.0)
        if ducking:
            chain = (
                "[0:v]{subs}[vout];"
                "[{bgm_idx}:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},afade=t=out:st={fade_st:.3f}:d=2,volume={bgm_vol:.4f}[bgm];"
                "[1:a]{pre}apad=whole_dur={dur:.3f},asplit[voice][sc];"
                "[bgm][sc]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=500:makeup=1[duck];"
                "[voice][duck]amix=inputs=2:duration=longest:normalize=0[premix]"
            ).format(subs=subtitles_filter, dur=dur, fade_st=fade_st, pre=prefilter, loud=loudnorm_filter,
                     bgm_idx=bgm_index, bgm_vol=bgm_volume)
        else:
            chain = (
                "[0:v]{subs}[vout];"
                "[{bgm_idx}:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},afade=t=out:st={fade_st:.3f}:d=2,volume={bgm_vol:.4f}[bgm];"
                "[1:a]{pre}apad=whole_dur={dur:.3f}[voice];"
                "[voice][bgm]amix=inputs=2:duration=longest:normalize=0[premix]"
            ).format(subs=subtitles_filter, dur=dur, fade_st=fade_st, pre=prefilter, loud=loudnorm_filter,
                     bgm_idx=bgm_index, bgm_vol=bgm_volume)
    else:
        chain = "[0:v]{subs}[vout];[1:a]{pre}apad=whole_dur={dur:.3f}[premix]".format(
            subs=subtitles_filter, dur=dur, pre=prefilter
        )

    sfx_filter_parts, sfx_labels = build_sfx_overlay_filters(sfx, next_index)
    for s in (sfx or []):
        cmd += ["-i", s["path"]]
    next_index += len(sfx or [])

    if sfx_labels:
        chain += ";" + ";".join(sfx_filter_parts)
        mix_inputs = "[premix]" + "".join(sfx_labels)
        chain += ";{mix_inputs}amix=inputs={n}:duration=longest:normalize=0[premix2]".format(
            mix_inputs=mix_inputs, n=1 + len(sfx_labels)
        )
        premix_label = "premix2"
    else:
        premix_label = "premix"

    chain += ";[{premix_label}]{loud}[aout]".format(premix_label=premix_label, loud=loudnorm_filter)

    cmd += ["-filter_complex", chain, "-map", "[vout]", "-map", "[aout]"]

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
