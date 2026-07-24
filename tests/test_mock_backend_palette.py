# -*- coding: utf-8 -*-
"""P2: mock_backend が reference_visual.color_palette_hex を gradients の c0/c1 に反映すること。
"""
from __future__ import annotations

from pipeline.visual import mock_backend


def _find_gradients_src(cmd):
    """gradients ソースは -f lavfi -i <src> として渡される（-vf ではない）。"""
    for i, a in enumerate(cmd):
        if a == "-i" and i + 1 < len(cmd) and str(cmd[i + 1]).startswith("gradients="):
            return cmd[i + 1]
    return ""


def test_palette_hex_flows_into_gradients_c0_c1(tmp_path):
    shot = {
        "id": "s1",
        "duration_sec": 3.0,
        "motion_preset": "static",
        "reference_visual": {
            "color_palette_hex": ["#f4e6d0", "#3b2a1a"],
        },
    }
    cmd = mock_backend.build_mock_cmd(
        "ffmpeg", shot, str(tmp_path / "out.mp4"), fonts_dir=str(tmp_path),
    )
    src = _find_gradients_src(cmd)
    assert "c0=0xF4E6D0" in src
    assert "c1=0x3B2A1A" in src


def test_single_color_palette_uses_fallback_second_color(tmp_path):
    """1色しか無い場合は既存 _PALETTE の 2 色目にフォールバックする。"""
    shot = {
        "id": "s1", "duration_sec": 3.0, "motion_preset": "static",
        "reference_visual": {"color_palette_hex": ["#ff0000"]},
    }
    cmd = mock_backend.build_mock_cmd(
        "ffmpeg", shot, str(tmp_path / "out.mp4"), fonts_dir=str(tmp_path),
    )
    src = _find_gradients_src(cmd)
    assert "c0=0xFF0000" in src
    # c1 は _PALETTE のフォールバック（"0x..." 形式）を使う
    assert "c1=0x" in src


def test_no_palette_uses_shot_id_based_default(tmp_path):
    """reference_visual が無い/palette 空なら shot_id ベースの循環パレットを使う（回帰確認）。"""
    shot = {"id": "s1", "duration_sec": 3.0, "motion_preset": "static"}
    cmd = mock_backend.build_mock_cmd(
        "ffmpeg", shot, str(tmp_path / "out.mp4"), fonts_dir=str(tmp_path),
    )
    src = _find_gradients_src(cmd)
    # _PALETTE の最初の色（藍→金）は 0x1a2a6c → shot_idハッシュに依存するが、
    # 少なくとも c0/c1 が両方 0x で始まる有効hexであること
    assert "c0=0x" in src and "c1=0x" in src
    # reference_visual 由来のパレット（#f4e6d0）が混入していないこと
    assert "0xF4E6D0" not in src


def test_invalid_hex_in_palette_is_ignored(tmp_path):
    shot = {
        "id": "s1", "duration_sec": 3.0, "motion_preset": "static",
        "reference_visual": {"color_palette_hex": ["red", "#zzzzzz", "#00ff00", "#0000ff"]},
    }
    cmd = mock_backend.build_mock_cmd(
        "ffmpeg", shot, str(tmp_path / "out.mp4"), fonts_dir=str(tmp_path),
    )
    src = _find_gradients_src(cmd)
    # 無効値は捨てて先頭2色を採用
    assert "c0=0x00FF00" in src
    assert "c1=0x0000FF" in src
