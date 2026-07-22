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


# --- animation_enabled（テロップのカットイン化） ---

_ANIM_PIECES = [
    {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト1"], "emphasis": [], "style": "base"},
    {"out_start": 2.0, "out_end": 4.0, "lines": ["テスト2"], "emphasis": [], "style": "big"},
]


def test_generate_ass_legacy_animation_enabled_false_has_no_fad_or_pop():
    """CLI経路（run.py）で使う legacy generate_ass も animation_enabled=False で
    アニメタグを一切付けない（[中]バグ修正: CLI経路配線）。
    """
    ass_text = subtitles.generate_ass(_ANIM_PIECES, animation_enabled=False)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert dialogue_lines
    for line in dialogue_lines:
        assert "\\fad(" not in line
        assert "\\t(" not in line
        assert "\\fscx" not in line
        assert "\\fscy" not in line
    assert "テスト1" in ass_text and "テスト2" in ass_text


def test_generate_ass_with_style_animation_enabled_true_keeps_fad_and_pop():
    ass_text = subtitles.generate_ass_with_style(_ANIM_PIECES, animation_enabled=True)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert dialogue_lines
    for line in dialogue_lines:
        assert "\\fad(" in line
        assert "\\fscx94\\fscy94\\t(0,120,\\fscx100\\fscy100)" in line


def test_generate_ass_with_style_default_is_animated_backward_compatible():
    """引数省略時は従来どおりアニメーション付き（完全後方互換）。"""
    ass_text = subtitles.generate_ass_with_style(_ANIM_PIECES)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert dialogue_lines
    for line in dialogue_lines:
        assert "\\fad(" in line


def test_build_telop_pieces_long_caption_30_chars_fits_in_two_balanced_lines():
    """BUG-54: 30字caption(MAX_CAPTION_CHARS上限)は幅ベース折り返しで2行に収まる。
    フォント段階縮小(最小48px)により最終描画幅が安全幅内に収まること・
    結合は原文と完全一致であること・font_px_override が既定より小さいこと。
    """
    caption = "あ" * 30
    shots = [{"id": "s1", "duration_sec": 5.0, "caption_jp": caption}]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 1
    lines = pieces[0]["lines"]
    assert len(lines) == 2, "30字は幅ベース+フォント縮小で必ず2行に収まるべき"
    assert "".join(lines) == caption
    # 各行の実描画幅が安全幅(852px)内に収まっている
    effective = pieces[0].get("font_px_override", subtitles.STYLE_BIG_FONTSIZE)
    safe = subtitles.caption_safe_width_px()
    for ln in lines:
        assert subtitles._line_visual_width_px(ln, effective) <= safe + 1e-6, (
            "行が幅を超えている: {} chars * {}px".format(len(ln), effective)
        )
    # 幅ベース縮小が実際に効いていること(hook shot なので default=92から下がる)
    assert effective <= subtitles.STYLE_BIG_FONTSIZE
    assert effective >= subtitles.CAPTION_MIN_FONT_PX


def test_build_telop_pieces_clamps_caption_out_offset_beyond_shot_duration():
    """BUG-55: caption_out_offset_sec が shot の duration_sec を越える場合、
    build_telop_pieces_from_shots が out_end を shot_end に厳密クランプする。
    (音声主導タイミング同期モードで表示尺がplan尺より縮んだ場合の安全網)。
    """
    shots = [
        # caption_out_offset_sec (5.5) が duration_sec (3.0) を明らかに超えるケース
        {"id": "s1", "duration_sec": 3.0, "caption_jp": "早いフック",
         "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 5.5},
        {"id": "s2", "duration_sec": 2.0, "caption_jp": "次のセリフ"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    # s1: out_start=0.5, out_end=shot_end=3.0 (5.5にはならない)
    assert pieces[0]["out_start"] == 0.5
    assert pieces[0]["out_end"] == 3.0
    # s2: 累積開始=3.0
    assert pieces[1]["out_start"] == 3.0
    # 隣接captionが重ならない: 前.end <= 次.start
    assert pieces[0]["out_end"] <= pieces[1]["out_start"] + 1e-6


def test_build_telop_pieces_clamps_caption_in_offset_beyond_shot_duration():
    """BUG-55: caption_in_offset_sec が duration_sec を越える異常plan でも out_start は
    shot_end で頭打ち(=次shot境界を跨がない)."""
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "テスト",
         "caption_in_offset_sec": 5.0, "caption_out_offset_sec": 8.0},
        {"id": "s2", "duration_sec": 2.0, "caption_jp": "次"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    # s1: out_start は shot_end=2.0 でクランプ、out_end も同じ (表示は事実上0秒 → validate_plan で弾かれる想定)
    assert pieces[0]["out_start"] <= 2.0 + 1e-6
    assert pieces[0]["out_end"] <= 2.0 + 1e-6


def test_wrap_caption_by_width_shrinks_font_when_default_overflows():
    """BUG-54: 幅ベース折り返しで既定フォントに収まらない場合はフォントを段階縮小する。"""
    caption = "あ" * 30
    lines, effective = subtitles.wrap_caption_by_width(caption, font_px=76)
    assert len(lines) <= 2
    assert "".join(lines) == caption
    assert effective <= 76
    assert effective >= subtitles.CAPTION_MIN_FONT_PX


def test_wrap_caption_by_width_returns_default_font_when_fits():
    """短文は既定フォントのまま単一行で返る(縮小しない)。"""
    caption = "短文"
    lines, effective = subtitles.wrap_caption_by_width(caption, font_px=76)
    assert lines == ["短文"]
    assert effective == 76


def test_build_telop_pieces_short_caption_no_mid_word_split():
    """BUG-2 の不変条件: 「毎日5分で散らかる前にリセット」の折り返しで「リセット」が
    語中分割(「リセ」/「ット」)されない。BUG-54で幅ベース折り返しに変わっても保持する。"""
    text = "毎日5分で散らかる前にリセット"
    shots = [{"id": "s1", "duration_sec": 5.0, "caption_jp": text}]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    lines = pieces[0]["lines"]
    assert "".join(lines) == text
    # 「リセット」が語中で切れていないこと。
    joined_check = "|".join(lines)
    assert "リセ|ット" not in joined_check
    for ln in lines:
        assert not ln.endswith("リセ")


def test_generate_ass_with_style_animation_disabled_has_no_fad_or_pop():
    """animation_enabled=False では \\fad / \\t / \\fscx / \\fscy を一切付けない（即時カットイン）。"""
    ass_text = subtitles.generate_ass_with_style(_ANIM_PIECES, animation_enabled=False)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert dialogue_lines
    for line in dialogue_lines:
        assert "\\fad(" not in line
        assert "\\t(" not in line
        assert "\\fscx" not in line
        assert "\\fscy" not in line
    # テロップ本文自体は保持される。
    assert "テスト1" in ass_text and "テスト2" in ass_text
