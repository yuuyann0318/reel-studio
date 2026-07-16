# -*- coding: utf-8 -*-
"""Reel Studio向けのpipeline/subtitles.py拡張（plan.subtitle_styleを反映したASS再生成）の単体テスト。

既存のtests/test_subtitles.py（既存挙動の維持）は無改修。
"""
from pipeline import subtitles


def test_resolve_subtitle_style_defaults_when_none():
    style = subtitles.resolve_subtitle_style(None)
    assert style == subtitles.DEFAULT_SUBTITLE_STYLE


def test_resolve_subtitle_style_partial_override_keeps_other_defaults():
    style = subtitles.resolve_subtitle_style({"font_size": 90})
    assert style["font_size"] == 90
    assert style["accent_color"] == subtitles.DEFAULT_SUBTITLE_STYLE["accent_color"]
    assert style["position"] == subtitles.DEFAULT_SUBTITLE_STYLE["position"]


def test_build_ass_header_with_style_reflects_font_size():
    header = subtitles.build_ass_header_with_style({"font_size": 100, "position": "lower", "accent_color": "#FFD84D"})
    assert "Noto Sans JP Black,100," in header
    assert "Noto Sans JP Black,116," in header  # big_size = base+16（既存の76/92差分を踏襲）


def test_build_ass_header_with_style_position_changes_margin_v():
    lower = subtitles.build_ass_header_with_style({"position": "lower"})
    upper = subtitles.build_ass_header_with_style({"position": "upper"})
    assert lower != upper
    assert "420" in lower
    assert "1400" in upper


def test_build_ass_header_with_style_accent_color_applied_to_big_style():
    header = subtitles.build_ass_header_with_style({"accent_color": "#FFD400"})
    expected = subtitles._hex_to_ass_style_color("#FFD400")
    assert expected in header


def test_generate_ass_with_style_contains_header_and_dialogue():
    pieces = [{"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"], "emphasis": [], "style": "base"}]
    ass_text = subtitles.generate_ass_with_style(pieces, {"font_size": 80})
    assert "PlayResX: 1080" in ass_text
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,Base" in ass_text
    assert "Noto Sans JP Black,80," in ass_text
