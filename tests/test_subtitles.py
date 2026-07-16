# -*- coding: utf-8 -*-
from pipeline import subtitles


def test_wrap_caption_kinsoku_short_text_single_line():
    lines = subtitles.wrap_caption_kinsoku("短いテキスト")
    assert lines == ["短いテキスト"]


def test_wrap_caption_kinsoku_long_text_wraps_to_two_lines():
    text = "あ" * 20
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert len(lines) == 2
    assert lines[0] == "あ" * 13
    assert lines[1] == "あ" * 7


def test_wrap_caption_kinsoku_avoids_forbidden_line_start():
    text = "あいうえおかきくけこさしす。えおかき"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert not lines[1].startswith("。")


def test_build_telop_pieces_from_shots_sync_to_cumulative_duration():
    shots = [
        {"id": "s1", "duration_sec": 5.0, "caption_jp": "最初のキャプション"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "次のキャプション"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 2
    assert pieces[0]["out_start"] == 0.0
    assert pieces[0]["out_end"] == 5.0
    assert pieces[1]["out_start"] == 5.0
    assert pieces[1]["out_end"] == 8.0


def test_build_telop_pieces_skips_empty_caption():
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": ""},
        {"id": "s2", "duration_sec": 2.0, "caption_jp": "あり"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 1
    assert pieces[0]["out_start"] == 2.0


def test_build_telop_pieces_hook_shot_gets_big_style():
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "フック"},
        {"id": "s2", "duration_sec": 2.0, "caption_jp": "本編"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots, hook_shot_id="s1")
    assert pieces[0]["style"] == "big"
    assert pieces[1]["style"] == "base"


def test_generate_ass_contains_header_and_dialogue():
    pieces = [{"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"], "emphasis": [], "style": "base"}]
    ass_text = subtitles.generate_ass(pieces)
    assert "PlayResX: 1080" in ass_text
    assert "PlayResY: 1920" in ass_text
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,Base" in ass_text


def test_hex_to_ass_bgr_conversion():
    assert subtitles.hex_to_ass_bgr("#FFD400") == "&H00D4FF&"
