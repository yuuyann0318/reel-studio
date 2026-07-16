# -*- coding: utf-8 -*-
import subprocess

import pytest

from pipeline.config import project_root
from pipeline.visual import mock_backend
from pipeline.visual.base import VisualBackendError

FFMPEG_BIN = str(project_root() / "bin" / "ffmpeg")
FFPROBE_BIN = str(project_root() / "bin" / "ffprobe")


def _shot(**overrides):
    base = {
        "id": "s1",
        "visual_prompt": "abstract background",
        "motion_preset": "zoom_in",
        "duration_sec": 1.5,
        "caption_jp": "テストキャプション",
    }
    base.update(overrides)
    return base


def test_build_mock_cmd_structure():
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, _shot(), "/tmp/out.mp4")
    assert cmd[0] == FFMPEG_BIN
    assert "-f" in cmd and "lavfi" in cmd
    assert cmd[-1] == "/tmp/out.mp4"
    assert "-vf" in cmd


def test_motion_exprs_all_presets_produce_valid_strings():
    for preset in ("static", "pan_left", "pan_right", "zoom_in", "zoom_out", "ken_burns", "unknown_preset"):
        z, x, y = mock_backend._motion_exprs(preset, total_frames=90)
        assert isinstance(z, str) and z
        assert isinstance(x, str) and x
        assert isinstance(y, str) and y


def test_escape_drawtext_handles_special_chars():
    escaped = mock_backend._escape_drawtext("50%オフ: 'すごい'")
    assert "\\%" in escaped
    assert "\\:" in escaped
    assert "'" not in escaped.replace("’", "")  # シングルクォートは全角へ置換済み


def test_shot_number_label_extracts_digits():
    assert mock_backend._shot_number_label("s1") == "#1"
    assert mock_backend._shot_number_label("s12") == "#12"
    assert mock_backend._shot_number_label("") == ""
    assert mock_backend._shot_number_label(None) == ""


def test_build_mock_cmd_does_not_burn_caption_text_bug1():
    """BUG-1: mockクリップにcaption_jpをdrawtextで焼き込まない（ASS字幕と二重表示になるため）。"""
    shot = _shot(caption_jp="このキャプションは画面に焼き込まれてはならない")
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, shot, "/tmp/out.mp4")
    vf = cmd[cmd.index("-vf") + 1]
    assert "このキャプションは画面に焼き込まれてはならない" not in vf
    # ショット識別用の小さな番号表示は残る
    assert "drawtext" in vf
    assert "#1" in vf


def test_build_mock_cmd_no_drawtext_when_shot_id_missing():
    shot = _shot(id="")
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, shot, "/tmp/out.mp4")
    vf = cmd[cmd.index("-vf") + 1]
    assert "drawtext" not in vf


@pytest.mark.slow
def test_mock_backend_generate_produces_valid_clip(tmp_path):
    """実機ffmpegを使い小尺(1.5秒)のクリップを実際に生成して検証する（重い部分は小尺に限定）。"""
    out_path = tmp_path / "s1.mp4"
    backend = mock_backend.MockBackend({"ffmpeg_bin": FFMPEG_BIN})
    meta = backend.generate(_shot(duration_sec=1.5), str(out_path))
    assert meta["backend"] == "mock"
    assert out_path.exists()
    assert out_path.stat().st_size > 0

    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "stream=width,height,codec_type",
         "-of", "default=noprint_wrappers=1", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out = proc.stdout.decode("utf-8")
    assert "width=1080" in out
    assert "height=1920" in out
