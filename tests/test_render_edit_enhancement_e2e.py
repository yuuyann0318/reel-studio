# -*- coding: utf-8 -*-
"""edit enhancement層の実ffmpeg実行E2Eテスト（bin/ffmpeg・bin/ffprobe）。

2ショット分の合成映像(lavfi testsrc)を正規化・連結し、ナレーション(無音)+BGM(sine)+
カットSE(実在するassets/sfx/whoosh_01.wav)+BGM音量カーブを載せて最終レンダリングし、
出力mp4が実際に生成されること・ffprobeの実測で映像/音声ストリームと尺が妥当なことを検証する。
純関数のフィルタ文字列構築が実際のffmpegで壊れずに動くことの実証（他のテストはすべてコマンド
文字列の検証のみで、実行はしない）。
"""
import subprocess

import pytest

from pipeline import edit_profile
from pipeline import subtitles
from pipeline.config import project_root
from pipeline import render

_FFMPEG_BIN = str(project_root() / "bin" / "ffmpeg")
_FFPROBE_BIN = str(project_root() / "bin" / "ffprobe")
_FONTS_DIR = str(project_root() / "assets" / "fonts")
_WHOOSH_PATH = str(project_root() / "assets" / "sfx" / "whoosh_01.wav")


def _make_testsrc_clip(path, duration_sec, color):
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi",
            "-i", "color=c={}:s=320x240:d={:.3f}:r=30".format(color, duration_sec),
            "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _make_silence(path, duration_sec):
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t", "{:.3f}".format(duration_sec), "-c:a", "pcm_s16le", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _make_bgm(path, duration_sec):
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration={:.3f}".format(duration_sec),
            "-c:a", "pcm_s16le", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _ffprobe_json(path, entries):
    proc = subprocess.run(
        [_FFPROBE_BIN, "-v", "error", "-show_entries", entries, "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace").strip()


@pytest.mark.slow
def test_final_render_with_cut_sfx_and_bgm_curve_produces_valid_mp4(tmp_path):
    shot_durations = [2.0, 1.5]

    # --- 2ショット分の映像を正規化(build_normalize_clip_cmdを実際に通す)して連結 ---
    clip_paths = []
    for i, (dur, color) in enumerate(zip(shot_durations, ["red", "blue"])):
        raw_path = tmp_path / "raw{}.mp4".format(i)
        _make_testsrc_clip(raw_path, dur, color)
        norm_path = tmp_path / "norm{}.mp4".format(i)
        cmd = render.build_normalize_clip_cmd(_FFMPEG_BIN, str(raw_path), str(norm_path), duration_sec=dur)
        res = render.run_ffmpeg(cmd, timeout_sec=60)
        assert res["returncode"] == 0, res["stderr"]
        clip_paths.append(str(norm_path))

    list_path = tmp_path / "list.txt"
    list_path.write_text(render.build_concat_list_content(clip_paths), encoding="utf-8")
    concat_path = tmp_path / "concat.mp4"
    res = render.run_ffmpeg(render.build_concat_cmd(_FFMPEG_BIN, str(list_path), str(concat_path)), timeout_sec=60)
    assert res["returncode"] == 0, res["stderr"]

    out_duration = sum(shot_durations)

    # --- ナレーション(無音)・BGM(sine)・字幕(空)を用意 ---
    narration_path = tmp_path / "narration.wav"
    _make_silence(narration_path, out_duration)
    bgm_path = tmp_path / "bgm.wav"
    _make_bgm(bgm_path, out_duration + 2.0)
    ass_path = tmp_path / "subs.ass"
    ass_path.write_text(subtitles.generate_ass([]), encoding="utf-8")

    # --- edit_profileをロードし、edit enhancement kwargsを実際に計算 ---
    edit_prof = edit_profile.load_edit_profile()
    assert edit_prof["cut_sfx"]["resolved_path"] == _WHOOSH_PATH
    enhancement = render.compute_edit_enhancement_kwargs(shot_durations, edit_prof)
    assert len(enhancement["sfx_extra"]) == 1  # 2ショット目境界(2.0秒)のみカットSEが立つ
    assert enhancement["sfx_extra"][0]["at_sec"] == 2.0
    assert enhancement["bgm_curve"] is not None

    output_path = tmp_path / "out.mp4"
    cmd = render.build_final_cmd(
        _FFMPEG_BIN, str(concat_path), str(narration_path), str(output_path), str(ass_path), _FONTS_DIR,
        bgm_path=str(bgm_path), out_duration=out_duration, bgm_gain_db=-8,
        sfx=enhancement["sfx_extra"], bgm_curve=enhancement["bgm_curve"],
        first_shot_impact_sec=enhancement["first_shot_impact_sec"],
    )
    res = render.run_ffmpeg(cmd, timeout_sec=120)
    assert res["returncode"] == 0, res["stderr"]
    assert output_path.exists()

    measured_duration = float(_ffprobe_json(output_path, "format=duration"))
    assert abs(measured_duration - out_duration) < 0.3

    v_codec = _ffprobe_json(output_path, "stream=codec_type")
    assert "video" in v_codec and "audio" in v_codec
