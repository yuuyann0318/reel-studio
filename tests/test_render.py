# -*- coding: utf-8 -*-
from pipeline import render


def test_build_concat_list_content_quotes_paths():
    content = render.build_concat_list_content(["/a/b.mp4", "/c/d's.mp4"])
    assert "file '/a/b.mp4'" in content
    assert "d'\\''s.mp4" in content


def test_build_normalize_clip_cmd_has_9x16_filter():
    cmd = render.build_normalize_clip_cmd("/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0)
    assert cmd[0] == "/bin/ffmpeg"
    assert "-an" in cmd
    assert "-t" in cmd
    joined = " ".join(cmd)
    assert "scale=1080:1920" in joined
    assert "crop=1080:1920" in joined


def test_build_concat_cmd_uses_copy_codec():
    cmd = render.build_concat_cmd("/bin/ffmpeg", "/list.txt", "/out.mp4")
    assert "-c" in cmd and "copy" in cmd
    assert cmd[-1] == "/out.mp4"


def test_build_loudnorm_filter_pass1_has_print_format_json():
    filt = render.build_loudnorm_filter(None)
    assert "measured_I" not in filt
    assert "print_format=json" in filt


def test_build_loudnorm_filter_pass2_includes_measured_values():
    measured = {
        "measured_I": "-20.0", "measured_TP": "-3.0", "measured_LRA": "5.0",
        "measured_thresh": "-30.0", "offset": "0.5",
    }
    filt = render.build_loudnorm_filter(measured)
    assert "measured_I=-20.0" in filt
    assert "linear=true" in filt


def test_build_final_cmd_without_bgm_has_apad_and_maps():
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0,
    )
    joined = " ".join(cmd)
    assert "apad=whole_dur=10.000" in joined
    assert "-map" in cmd and "[vout]" in cmd and "[aout]" in cmd
    assert "-i" in cmd and "/narration.wav" in cmd


def test_build_final_cmd_with_bgm_has_sidechaincompress():
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=10.0,
    )
    joined = " ".join(cmd)
    assert "sidechaincompress" in joined
    assert "/bgm.mp3" in cmd
    assert "amix=inputs=2:duration=longest" in joined


def test_escape_ffmpeg_filter_path_escapes_quote():
    escaped = render.escape_ffmpeg_filter_path("it's/a path.ass")
    assert escaped == "'it'\\''s/a path.ass'"
