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


def test_build_ass_header_with_style_big_primary_is_white_regardless_of_accent():
    """Big行の全文accent塗りは明るい背景で可読性が崩れるため廃止（実機目視で確定）。

    accent_colorを指定してもBig行のPrimaryは白のまま。強調は自動アクセントの部分着色で行う。
    """
    header = subtitles.build_ass_header_with_style({"accent_color": "#FFD400"})
    assert subtitles._hex_to_ass_style_color("#FFD400") not in header
    big_style_line = next(l for l in header.splitlines() if l.startswith("Style: Big"))
    assert "&H00FFFFFF" in big_style_line


def test_generate_ass_with_style_contains_header_and_dialogue():
    pieces = [{"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"], "emphasis": [], "style": "base"}]
    ass_text = subtitles.generate_ass_with_style(pieces, {"font_size": 80})
    assert "PlayResX: 1080" in ass_text
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,Base" in ass_text
    assert "Noto Sans JP Black,80," in ass_text


# --- ヘッダのデザイン定数（ダークチャコール縁・半透明黒のソフトシャドウ） -----------------

def test_build_ass_header_with_style_uses_charcoal_outline_and_soft_shadow_colors():
    header = subtitles.build_ass_header_with_style(None)
    assert subtitles.OUTLINE_COLOR in header  # &H001A1A1A（ダークチャコール）
    assert subtitles.BACK_COLOR in header  # &H80000000（半透明黒=ソフトシャドウ色）
    assert ",2.5,3," in header  # Outline=2.5, Shadow=3


# --- 全Dialogueへのフェード+ポップイン付与 --------------------------------------------

def test_generate_ass_with_style_all_dialogue_lines_have_fad_and_pop_override():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト1"], "emphasis": [], "style": "base"},
        {"out_start": 2.0, "out_end": 4.0, "lines": ["テスト2"], "emphasis": [], "style": "big"},
    ]
    ass_text = subtitles.generate_ass_with_style(pieces)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogue_lines) == 2
    for line in dialogue_lines:
        assert "\\fad(160,120)" in line
        assert "\\fscx94\\fscy94\\t(0,120,\\fscx100\\fscy100)" in line


# --- 自動アクセント抽出: 数字+単位 --------------------------------------------------

def test_extract_accent_spans_matches_number_with_unit():
    caption = "たった3日間で効果を実感できる方法です"
    spans = subtitles.extract_accent_spans(caption)
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "3日間"


# --- 自動アクセント抽出: 「」は中身のみ着色（括弧自体は白のまま） ------------------------

def test_extract_accent_spans_colors_bracket_content_only_not_the_brackets():
    caption = "「時短」がすごいポイントです"
    spans = subtitles.extract_accent_spans(caption)
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "時短"
    assert caption[start - 1] == "「"
    assert caption[end] == "」"


# --- 自動アクセント抽出: 商品名トークン ------------------------------------------------

def test_extract_accent_spans_matches_product_name_token():
    caption = "今日はKINUI枕を紹介します"
    spans = subtitles.extract_accent_spans(caption, product_name="KINUI 低反発枕")
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "KINUI"


# --- 自動アクセント抽出: 最大limit(既定2)件まで ---------------------------------------

def test_extract_accent_spans_caps_at_limit_two():
    caption = "3日間で5kg減って「効果」もすごいし2回使うだけ"
    spans = subtitles.extract_accent_spans(caption, limit=2)
    assert len(spans) == 2
    # 開始位置順に並んでいること
    assert spans[0][0] < spans[1][0]


# --- 自動アクセント抽出: 着色合計がcaption全長の60%を超える分は破棄 ------------------------

def test_extract_accent_spans_suppresses_spans_exceeding_60_percent_coverage():
    caption = "3日間5kg減"  # 7文字。「3日間」(3字)+「5kg」(3字)=6字は60%(4.2字)を超える
    spans = subtitles.extract_accent_spans(caption)
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "3日間"


# --- Big行も白ベース+自動アクセント（Primary白化により\cの白リセットで壊れない） --

# --- 独立レビュー対応: アクセント優先度がlimit選抜に効くこと ----------------------------

def test_extract_accent_spans_priority_wins_over_earlier_lower_priority_spans_when_limited():
    """「」(優先度低)と数字+単位が先に出現し、商品名(優先度最高)が後方に出現するcaptionで、
    limit選抜が開始位置ではなく優先度で行われ、商品名span が必ず採用されることを確認する。
    """
    caption = "「効果」も2回試せてKINUI枕がおすすめです"
    spans = subtitles.extract_accent_spans(caption, product_name="KINUI 低反発枕", limit=2)
    assert len(spans) == 2
    texts = [caption[start:end] for start, end in spans]
    assert "KINUI" in texts  # 後方の商品名(優先度最高)がlimit選抜から漏れない
    assert "効果" not in texts  # 優先度最低の「」は選抜から外れる
    # 返り値は開始位置順であること(呼び出し側の前提と互換)。
    assert spans[0][0] < spans[1][0]


# --- 独立レビュー対応: 商品名トークンの一般語誤爆防止(最小長3文字+ストップリスト) ---------

def test_product_name_candidates_excludes_short_and_stopword_tokens():
    candidates = subtitles._product_name_candidates("AI 美容 セット")
    assert "美容" not in candidates  # 2文字なのでmin_len未満
    assert "セット" not in candidates  # 3文字だがストップリスト該当
    assert "AI" not in candidates  # 2文字なのでmin_len未満
    assert "AI 美容 セット" in candidates  # フル文字列は従来どおり候補に残る


def test_extract_accent_spans_does_not_color_generic_word_from_product_name():
    caption = "美容が好きな人におすすめです"
    spans = subtitles.extract_accent_spans(caption, product_name="AI 美容ドライヤー")
    assert spans == []


def test_extract_accent_spans_still_colors_specific_product_token():
    caption = "今日は美容ドライヤーを使う"
    spans = subtitles.extract_accent_spans(caption, product_name="AI 美容ドライヤー")
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "美容ドライヤー"


# --- 独立レビュー対応: 西暦(4桁数字+年)は数字+単位アクセントの対象外 ----------------------

def test_extract_accent_spans_excludes_4digit_calendar_year():
    caption = "2026年に流行った商品はこちらです"
    spans = subtitles.extract_accent_spans(caption)
    assert spans == []


def test_extract_accent_spans_keeps_short_year_count():
    caption = "5年使える丈夫な素材でできています"
    spans = subtitles.extract_accent_spans(caption)
    assert len(spans) == 1
    start, end = spans[0]
    assert caption[start:end] == "5年"


def test_generate_ass_with_style_adds_accent_to_big_style_line_too():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["3日間で変わる"], "emphasis": [], "style": "big"},
        {"out_start": 2.0, "out_end": 4.0, "lines": ["3日間で変わる"], "emphasis": [], "style": "base"},
    ]
    ass_text = subtitles.generate_ass_with_style(pieces, {"accent_color": "#FFD84D"})
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    big_line = next(l for l in dialogue_lines if ",Big,,0,0,0,," in l)
    base_line = next(l for l in dialogue_lines if ",Base,,0,0,0,," in l)
    assert "\\c" in big_line  # フック行も部分アクセント（全文塗りの代替）
    assert "\\c" in base_line
