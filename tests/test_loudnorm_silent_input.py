# -*- coding: utf-8 -*-
"""無音入力での loudnorm 2パス即死バグ（BUG: measured_I=-inf out of range）の回帰テスト。

背景:
  ナレーション参考音声なし(TTSスキップ) + bgm_mode="none" + SFX空 のプロジェクトでは
  本編ミックスが完全無音になる。ffmpeg loudnorm 1パス目(print_format=json)は無音入力に
  対し input_i / input_tp / input_thresh を "-inf" と出力する。旧 _parse_loudnorm_json は
  この "-inf" をそのまま measured_I として 2パス目へ渡していたため、
    loudnorm: Value -inf for parameter 'measured_I' out of range [-99 - 0]
  で ffmpeg が即死し、最終レンダリングが失敗していた。

検証:
  (a) render.parse_loudnorm_json: -inf/範囲外/壊れた JSON を食わせて None（ユニット）
  (b) 「ナレ無し + bgm none」最小レンダを実 ffmpeg で 2パス相当実行し完走（統合・slow）
  (c) 音ありパス(BGM sine)は従来どおり有限 measured を得て 2パス適用でも完走（回帰・slow）
  (d) summarize_ffmpeg_error: Error/Invalid/failed 行を優先抽出して先頭へ付ける（ユニット）
"""
import math
import subprocess

import pytest

from pipeline.config import project_root
from pipeline import render
from pipeline import subtitles

_FFMPEG_BIN = str(project_root() / "bin" / "ffmpeg")
_FFPROBE_BIN = str(project_root() / "bin" / "ffprobe")
_FONTS_DIR = str(project_root() / "assets" / "fonts")


# 無音入力に対する実 ffmpeg loudnorm 1パス目 stderr（input_i 等が "-inf"）。
_SILENT_PASS1_STDERR = """
[Parsed_loudnorm_0 @ 0x600000]
{
\t"input_i" : "-inf",
\t"input_tp" : "-inf",
\t"input_lra" : "0.00",
\t"input_thresh" : "-inf",
\t"output_i" : "-inf",
\t"output_tp" : "-120.00",
\t"output_lra" : "0.00",
\t"output_thresh" : "-inf",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.00"
}
"""

_GOOD_PASS1_STDERR = """
[Parsed_loudnorm_0 @ 0x600000]
{
\t"input_i" : "-25.53",
\t"input_tp" : "-3.20",
\t"input_lra" : "7.10",
\t"input_thresh" : "-35.80",
\t"target_offset" : "0.50"
}
"""


# ---------------------------------------------------------------------------
# (a) / (d) ユニット
# ---------------------------------------------------------------------------

def test_parse_loudnorm_json_rejects_silent_inf():
    """無音入力の -inf 計測値は None を返す（→ 呼び出し側で単パスへフォールバック）。"""
    assert render.parse_loudnorm_json(_SILENT_PASS1_STDERR) is None


def test_parse_loudnorm_json_accepts_finite_measured():
    """有限かつ有効範囲内の計測値は dict を返す（従来動作の回帰）。"""
    measured = render.parse_loudnorm_json(_GOOD_PASS1_STDERR)
    assert measured == {
        "measured_I": "-25.53",
        "measured_TP": "-3.20",
        "measured_LRA": "7.10",
        "measured_thresh": "-35.80",
        "offset": "0.50",
    }
    # 実際に float 化でき、build_loudnorm_filter に埋め込んでも壊れないこと。
    for k in ("measured_I", "measured_TP", "measured_LRA", "measured_thresh", "offset"):
        assert math.isfinite(float(measured[k]))


def test_parse_loudnorm_json_rejects_out_of_range():
    """measured_I が範囲外(>0)なら None（loudnorm の [-99,0] を外れる値は 2パス目で即死する）。"""
    oor = _GOOD_PASS1_STDERR.replace('"input_i" : "-25.53"', '"input_i" : "5.00"')
    assert render.parse_loudnorm_json(oor) is None


def test_parse_loudnorm_json_rejects_nan_lra():
    """input_lra が nan でも None（非有限は全パラメータで弾く）。"""
    nan_txt = _GOOD_PASS1_STDERR.replace('"input_lra" : "7.10"', '"input_lra" : "nan"')
    assert render.parse_loudnorm_json(nan_txt) is None


def test_parse_loudnorm_json_returns_none_on_no_match_or_broken():
    assert render.parse_loudnorm_json("") is None
    assert render.parse_loudnorm_json(None) is None
    assert render.parse_loudnorm_json("ffmpeg version 6.0\nno json block here") is None
    # input_i はあるが JSON として壊れている
    assert render.parse_loudnorm_json('{ "input_i" : bogus }') is None


def test_summarize_ffmpeg_error_prioritizes_error_lines():
    """バナーで埋まった stderr でも Error/failed 行を先頭に持ち上げる。"""
    stderr = (
        "ffmpeg version 6.0 Copyright ...\n"
        "  built with clang\n"
        "Input #0, mov, from 'concat.mp4':\n"
        "  Stream #0:0: Video: h264\n"
        "[Parsed_loudnorm_0 @ 0x] Value -inf for parameter 'measured_I' out of range [-99 - 0]\n"
        "[AVFilterGraph] Error applying option 'measured_I' to filter 'loudnorm'\n"
        "Error initializing complex filters.\n"
        "Conversion failed!\n"
    )
    out = render.summarize_ffmpeg_error(stderr)
    assert out.startswith("[ffmpeg error] ")
    head = out.split("\n---\n", 1)[0]
    assert "measured_I" in head
    assert "Conversion failed!" in head
    # 元の末尾テールも残す（デバッグ用の文脈）。
    assert "---" in out


def test_summarize_ffmpeg_error_falls_back_to_tail_when_no_error_line():
    stderr = "just a plain banner\nwith no problem keywords\n"
    out = render.summarize_ffmpeg_error(stderr)
    assert not out.startswith("[ffmpeg error]")
    assert "plain banner" in out


# ---------------------------------------------------------------------------
# (b) / (c) 実 ffmpeg 統合（slow）
# ---------------------------------------------------------------------------

def _make_silent_color_clip(path, duration_sec, color="black"):
    """音声トラックを持たない色ベタ映像クリップ（[0:v] 想定）。"""
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi",
            "-i", "color=c={}:s=320x240:d={:.3f}:r=30".format(color, duration_sec),
            "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _make_silence_wav(path, duration_sec):
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi",
            "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t", "{:.3f}".format(duration_sec), "-c:a", "pcm_s16le", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _make_sine_wav(path, duration_sec, freq=440):
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-f", "lavfi",
            "-i", "sine=frequency={}:duration={:.3f}".format(freq, duration_sec),
            "-c:a", "pcm_s16le", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _ffprobe(path, entries):
    proc = subprocess.run(
        [_FFPROBE_BIN, "-v", "error", "-show_entries", entries, "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace").strip()


def _build_concat(tmp_path, out_duration):
    raw = tmp_path / "raw.mp4"
    _make_silent_color_clip(raw, out_duration)
    norm = tmp_path / "norm.mp4"
    res = render.run_ffmpeg(
        render.build_normalize_clip_cmd(_FFMPEG_BIN, str(raw), str(norm), duration_sec=out_duration),
        timeout_sec=60,
    )
    assert res["returncode"] == 0, res["stderr"]
    list_path = tmp_path / "list.txt"
    list_path.write_text(render.build_concat_list_content([str(norm)]), encoding="utf-8")
    concat = tmp_path / "concat.mp4"
    res = render.run_ffmpeg(render.build_concat_cmd(_FFMPEG_BIN, str(list_path), str(concat)), timeout_sec=60)
    assert res["returncode"] == 0, res["stderr"]
    return concat


def _run_two_pass(tmp_path, concat_path, narration_wav, bgm_path, out_duration):
    """production(run.py / jobs.py)と同一の 2パス loudnorm 経路を再現して実行する。"""
    ass_path = tmp_path / "subs.ass"
    ass_path.write_text(subtitles.generate_ass([]), encoding="utf-8")

    pass1 = tmp_path / "_loudnorm_pass1.mp4"
    cmd1 = render.build_final_cmd(
        _FFMPEG_BIN, str(concat_path), str(narration_wav), str(pass1), str(ass_path), _FONTS_DIR,
        bgm_path=(str(bgm_path) if bgm_path else None), out_duration=out_duration, loudnorm_measured=None,
    )
    res1 = render.run_ffmpeg(cmd1, timeout_sec=120)
    measured = render.parse_loudnorm_json(res1["stderr"])

    output = tmp_path / "out.mp4"
    cmd2 = render.build_final_cmd(
        _FFMPEG_BIN, str(concat_path), str(narration_wav), str(output), str(ass_path), _FONTS_DIR,
        bgm_path=(str(bgm_path) if bgm_path else None), out_duration=out_duration,
        loudnorm_measured=measured,
    )
    res2 = render.run_ffmpeg(cmd2, timeout_sec=120)
    return res2, output, measured


@pytest.mark.slow
def test_silent_narration_no_bgm_two_pass_completes(tmp_path):
    """(b) ナレ無し(無音wav) + bgm none の 2パスが実 ffmpeg で完走し mp4 が出る。

    無音 → 1パス目 measured_I=-inf → parse_loudnorm_json が None → 2パス目は単パス
    loudnorm(measured 無し)で走り、-inf 範囲外エラーで死なないことの実証。
    """
    out_duration = 3.0
    concat = _build_concat(tmp_path, out_duration)
    narration = tmp_path / "narration.wav"
    _make_silence_wav(narration, out_duration)

    res2, output, measured = _run_two_pass(tmp_path, concat, narration, None, out_duration)

    # 無音入力なので 1パス目の計測は破棄され measured は None（=単パスへフォールバック）。
    assert measured is None
    assert res2["returncode"] == 0, res2["stderr"]
    assert output.exists() and output.stat().st_size > 0
    measured_duration = float(_ffprobe(output, "format=duration"))
    assert abs(measured_duration - out_duration) < 0.3
    codecs = _ffprobe(output, "stream=codec_type")
    assert "video" in codecs and "audio" in codecs


@pytest.mark.slow
def test_audio_present_two_pass_applies_measured(tmp_path):
    """(c) 音あり(BGM sine)は有限 measured を得て 2パス目に適用でき、従来どおり完走する。"""
    out_duration = 3.0
    concat = _build_concat(tmp_path, out_duration)
    narration = tmp_path / "narration.wav"
    _make_silence_wav(narration, out_duration)
    bgm = tmp_path / "bgm.wav"
    _make_sine_wav(bgm, out_duration + 2.0)

    res2, output, measured = _run_two_pass(tmp_path, concat, narration, bgm, out_duration)

    # BGM に音があるので 1パス目の計測は有限・有効で、2パス目に measured が適用される。
    assert measured is not None, "音ありなのに measured が None（回帰）"
    assert math.isfinite(float(measured["measured_I"]))
    assert res2["returncode"] == 0, res2["stderr"]
    assert output.exists() and output.stat().st_size > 0
    codecs = _ffprobe(output, "stream=codec_type")
    assert "video" in codecs and "audio" in codecs
