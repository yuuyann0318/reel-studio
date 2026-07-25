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


# --- image_path分岐（image-to-video風のMock） ----------------------------------

def test_build_mock_cmd_without_image_path_uses_gradients_regression():
    """image_path未指定なら従来どおりgradientsソースを使う（回帰）。"""
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, _shot(), "/tmp/out.mp4")
    assert cmd[2] == "-f" and cmd[3] == "lavfi"
    assert "gradients=" in cmd[5]
    assert "-loop" not in cmd


def test_build_mock_cmd_with_missing_image_file_falls_back_to_gradients():
    """image_pathが指定されていてもファイルが実在しなければgradientsにフォールバックする(非エラー)。"""
    shot = _shot(image_path="/tmp/does-not-exist-xyz.png")
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, shot, "/tmp/out.mp4")
    assert cmd[2] == "-f" and cmd[3] == "lavfi"
    assert "-loop" not in cmd


def test_build_mock_cmd_with_existing_image_uses_image_input(tmp_path):
    image_path = tmp_path / "product.png"
    image_path.write_bytes(b"fake-png-bytes")
    shot = _shot(image_path=str(image_path))
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, shot, "/tmp/out.mp4")
    assert cmd[2] == "-loop" and cmd[3] == "1"
    assert cmd[4] == "-i" and cmd[5] == str(image_path)
    assert "-f" not in cmd[:2]
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in vf
    assert "crop=1080:1920" in vf
    assert "zoompan" in vf  # Ken Burns相当の動きは維持


def test_build_mock_cmd_with_existing_image_keeps_shot_number_label(tmp_path):
    image_path = tmp_path / "product.png"
    image_path.write_bytes(b"fake-png-bytes")
    shot = _shot(image_path=str(image_path), id="s3")
    cmd = mock_backend.build_mock_cmd(FFMPEG_BIN, shot, "/tmp/out.mp4")
    vf = cmd[cmd.index("-vf") + 1]
    assert "drawtext" in vf
    assert "#3" in vf


@pytest.mark.slow
def test_mock_backend_generate_with_image_path_produces_valid_clip(tmp_path):
    """実機ffmpegで image_path 指定時のクリップを実際に生成し、1080x1920のmp4が出ることをffprobeで実測する。"""
    image_path = tmp_path / "product.png"
    gen_proc = subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x480:rate=1",
            "-frames:v", "1", str(image_path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert gen_proc.returncode == 0, gen_proc.stderr.decode("utf-8", "replace")
    assert image_path.exists()

    out_path = tmp_path / "s1.mp4"
    backend = mock_backend.MockBackend({"ffmpeg_bin": FFMPEG_BIN})
    meta = backend.generate(_shot(duration_sec=1.5, image_path=str(image_path)), str(out_path))
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


# --- persona_anchor: mock はメタ記録のみ（実 identity 統一は higgsfield 側） -------

def _fake_ok_run(monkeypatch):
    class _P:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())


def test_mock_shot_has_person_detects():
    assert mock_backend._mock_shot_has_person({"has_person": True}) is True
    assert mock_backend._mock_shot_has_person({"reference_visual": {"has_person": True}}) is True
    assert mock_backend._mock_shot_has_person({"id": "s1"}) is False


def test_mock_generate_records_persona_meta_chain(monkeypatch, tmp_path):
    _fake_ok_run(monkeypatch)
    backend = mock_backend.MockBackend({})
    m1 = backend.generate(_shot(id="s1", has_person=True), str(tmp_path / "a.mp4"))
    m2 = backend.generate(_shot(id="s2", has_person=True), str(tmp_path / "b.mp4"))
    m3 = backend.generate(_shot(id="s3"), str(tmp_path / "c.mp4"))
    assert m1["persona_anchor"] == "captured"   # 最初の人物 shot
    assert m2["persona_anchor"] == "applied"     # 2本目以降
    assert m3["persona_anchor"] == "none"        # 非人物 shot


def test_mock_generate_persona_disabled(monkeypatch, tmp_path):
    _fake_ok_run(monkeypatch)
    backend = mock_backend.MockBackend({"visual": {"persona_consistency": False}})
    m = backend.generate(_shot(id="s1", has_person=True), str(tmp_path / "a.mp4"))
    assert m["persona_anchor"] == "none"
