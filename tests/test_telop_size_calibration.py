# -*- coding: utf-8 -*-
"""テロップサイズ較正（実ペア第2弾診断 #4）の実測検証。

診断: 参考グリフ高さ 5.8-6.1%H に対し生成は 2.2-2.8%H（既定 76px）まで縮んでいた。
較正後は、size_pct 欠落時の既定フォントおよび size_pct=6.0 の写像が、実測グリフ高さ
5.5-6.5%H に収まることを PIL で機械検証する。
"""
import os

import pytest

from pipeline import telop_style

PLAY_RES_Y = 1920
_FONT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts", "NotoSansJP-Black.ttf")


def _glyph_pct_h(font_px):
    """指定 px の NotoSansJP-Black で代表漢字グリフ bbox 高の %H を PIL 実測する。"""
    from PIL import ImageFont
    f = ImageFont.truetype(_FONT, int(font_px))
    # 代表的な本文漢字（bbox が最大近傍になる字）で測る。
    heights = []
    for ch in ("肌", "美", "艶", "顔"):
        b = f.getbbox(ch)
        heights.append(b[3] - b[1])
    gh = max(heights)
    return 100.0 * gh / PLAY_RES_Y


@pytest.mark.skipif(not os.path.isfile(_FONT), reason="font not found")
def test_default_font_glyph_height_in_target_band():
    """size_pct 欠落時の既定フォントが 5.5-6.5%H に収まる。"""
    px = telop_style.size_pct_to_font_px(None, PLAY_RES_Y)
    pct = _glyph_pct_h(px)
    assert 5.5 <= pct <= 6.5, "default px={} glyph={:.2f}%H".format(px, pct)


@pytest.mark.skipif(not os.path.isfile(_FONT), reason="font not found")
def test_size_pct_6_maps_into_target_band():
    """参考実測 size_pct=6.0 の写像が 5.5-6.5%H に収まる（従属＝写像の精度）。"""
    px = telop_style.size_pct_to_font_px(6.0, PLAY_RES_Y)
    pct = _glyph_pct_h(px)
    assert 5.5 <= pct <= 6.5, "size_pct=6.0 px={} glyph={:.2f}%H".format(px, pct)


def test_upper_clamp_caps_giant_size_pct():
    """特大 size_pct でも上限 px でクランプされる（2行×13字セーフゾーン保護）。"""
    assert telop_style.size_pct_to_font_px(30.0, PLAY_RES_Y) == telop_style._MAX_TELOP_FONT_PX


def test_default_is_much_larger_than_old_76():
    """回帰防止: 既定が旧 76px より十分大きい（≒参考サイズ帯）。"""
    assert telop_style.DEFAULT_TELOP_FONT_PX >= 110
