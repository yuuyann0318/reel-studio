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


def test_wrap_caption_kinsoku_bug2_does_not_split_word_mid_way():
    """BUG-2: 「毎日5分で散らかる前にリセット」が「リセ」/「ット」のように語中分割されない。

    行長バランス選択（|1行目-2行目|最小）により、文節境界候補[5, 11]のうち
    diff最小のi=5（5文字/10文字, diff=5）が選ばれる（i=11はdiff=7で不採用）。
    「リセット」は語中分割されず1行にまとまる、という不変条件は維持する。"""
    text = "毎日5分で散らかる前にリセット"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert len(lines) == 2
    assert "".join(lines) == text
    assert "リセット" in lines[1] or "リセット" in lines[0]
    assert "リセ" != lines[0][-2:]  # 「リセ」で行が終わっていない(=「ット」だけ次行に落ちていない)
    assert lines[0] == "毎日5分で"
    assert lines[1] == "散らかる前にリセット"


def test_find_bunsetsu_break_prefers_particle_boundary():
    cut = subtitles._find_bunsetsu_break("毎日5分で散らかる前にリセット", 13)
    assert cut == 11  # 「毎日5分で散らかる前に」で区切る


def test_wrap_caption_kinsoku_bug5_particle_de_not_confused_with_verb_dekiru():
    """BUG-5: 「できる」の先頭「で」を助詞と誤認して「毎日5分でで／きる」のように
    語中分割しない。助詞の直後がひらがな（=動詞/補助動詞の一部の可能性）の場合は
    その境界を無効化する。"""
    text = "毎日5分でできる片付け習慣を始めよう"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert len(lines) == 2
    assert "".join(lines) == text
    assert "できる" in lines[0]
    assert not lines[0].endswith("で")


def test_wrap_caption_kinsoku_bug5_does_not_regress_bug2():
    """BUG-5修正後もBUG-2（「リセ」/「ット」の語中分割）が再発しないことを確認する。"""
    text = "毎日5分で散らかる前にリセット"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert len(lines) == 2
    assert "".join(lines) == text
    assert "リセット" in lines[1]  # 「リセット」が語中分割されず1行にまとまっている


def test_wrap_caption_kinsoku_bug5_breaks_at_natural_position():
    """読点などの自然な位置で折れ、「範囲」「決める」が語中分割されない。"""
    text = "今日は床の上だけ、と範囲を決める"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert len(lines) == 2
    assert "".join(lines) == text
    assert ("範囲" in lines[0]) or ("範囲" in lines[1])
    assert ("決める" in lines[0]) or ("決める" in lines[1])


def test_wrap_caption_kinsoku_bug5_short_text_no_wrap_even_with_particle():
    """max_chars以内に収まる短文は、途中に助詞（「は」等）を含んでいても改行しない。"""
    text = "今日は片付けよう"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert lines == [text]


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


def test_wrap_caption_kinsoku_balanced_break_representative_example():
    """行長バランス選択の代表例: 候補[6,9,13]のうちdiff最小(=0)のi=9が選ばれ、
    2行がぴったり9文字ずつに揃う（「上品モダン」の行長揃え）。"""
    text = "毎日30分の運動を続けると体が変わる"
    lines = subtitles.wrap_caption_kinsoku(text, max_chars=13, max_lines=2)
    assert "".join(lines) == text
    assert lines == ["毎日30分の運動を", "続けると体が変わる"]
    assert len(lines[0]) == len(lines[1]) == 9


def test_generate_ass_all_dialogue_lines_have_fad_and_pop_override():
    """legacy generate_ass()（CLI経路）も build_dialogue_line 共通化により、
    全DialogueにASSのフェード+ポップイン({\\fad(...)\\fscx94\\fscy94\\t(0,120,\\fscx100\\fscy100)})
    が付与される。"""
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト1"], "emphasis": [], "style": "base"},
        {"out_start": 2.0, "out_end": 4.0, "lines": ["テスト2"], "emphasis": [], "style": "big"},
    ]
    ass_text = subtitles.generate_ass(pieces)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogue_lines) == 2
    for line in dialogue_lines:
        assert "\\fad(160,120)" in line
        assert "\\fscx94\\fscy94\\t(0,120,\\fscx100\\fscy100)" in line
