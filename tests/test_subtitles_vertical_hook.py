# -*- coding: utf-8 -*-
"""pipeline/subtitles.py の vertical_hook スタイル（縦書きテロップTTP再現）の単体テスト。

参考動画（576x1024・36.9秒・カット18回=平均約2秒）の様式を再現するスタイル。
ASSには真の縦組みがないため、1文字ずつ\\N改行して縦1列に積む簡易手法＋\\posでの
列単位の絶対配置により疑似縦書きを実現する。
"""
from pipeline import subtitles


def test_resolve_subtitle_style_defaults_preset_to_default():
    style = subtitles.resolve_subtitle_style(None)
    assert style["preset"] == "default"


def test_resolve_subtitle_style_accepts_vertical_hook_preset():
    style = subtitles.resolve_subtitle_style({"preset": "vertical_hook"})
    assert style["preset"] == "vertical_hook"


def test_resolve_subtitle_style_normalizes_unknown_preset_to_default():
    style = subtitles.resolve_subtitle_style({"preset": "not_a_real_preset"})
    assert style["preset"] == "default"


def test_split_into_vertical_columns_single_column_when_short():
    columns = subtitles.split_into_vertical_columns("今話題のAI動画")
    assert len(columns) == 1
    assert "".join(columns[0]) == "今話題のAI動画"


def test_split_into_vertical_columns_overflows_to_second_column():
    # 12文字 -> 最大10字/列を超えるため2列になる。
    text = "あいうえおかきくけこさし"
    assert len(text) == 12
    columns = subtitles.split_into_vertical_columns(text, max_col_chars=10)
    assert len(columns) == 2
    assert "".join(columns[0]) == text[:10]
    assert "".join(columns[1]) == text[10:]


def test_split_into_vertical_columns_empty_text_returns_empty_list():
    assert subtitles.split_into_vertical_columns("") == []


def test_prepare_vertical_hook_text_replaces_long_dash_with_vertical_bar():
    # 実機レンダリングで、長音「ー」を1文字ずつ縦積みすると横棒のまま不自然に見えることを
    # 確認したため、縦棒に見える全角「｜」へ置換する（BUG-10とは別件・vertical_hook固有の対応）。
    chars = subtitles._prepare_vertical_hook_text("結果ー")
    assert chars == ["結", "果", "｜"]


def test_build_vertical_hook_dialogue_lines_right_anchor_uses_right_x():
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 2.0, "今話題", anchor="right")
    assert len(lines) == 1
    assert "\\pos({},{})".format(subtitles.VERTICAL_HOOK_RIGHT_X, subtitles.VERTICAL_HOOK_TOP_Y) in lines[0]
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:02.00,VerticalHook,,0,0,0,,")


def test_build_vertical_hook_dialogue_lines_left_anchor_uses_left_x():
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 2.0, "今話題", anchor="left")
    assert "\\pos({},{})".format(subtitles.VERTICAL_HOOK_LEFT_X, subtitles.VERTICAL_HOOK_TOP_Y) in lines[0]


def test_build_vertical_hook_dialogue_lines_stacks_chars_with_ass_linebreak():
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 2.0, "AI", anchor="right")
    assert lines[0].endswith("A\\NI")


def test_build_vertical_hook_dialogue_lines_second_column_moves_left_of_first_when_right_anchored():
    text = "あいうえおかきくけこさし"  # 12文字 -> 2列
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 2.0, text, anchor="right")
    assert len(lines) == 2
    x1 = subtitles.VERTICAL_HOOK_RIGHT_X
    x2 = subtitles.VERTICAL_HOOK_RIGHT_X - subtitles.VERTICAL_HOOK_COL_STEP
    assert "\\pos({},{})".format(x1, subtitles.VERTICAL_HOOK_TOP_Y) in lines[0]
    assert "\\pos({},{})".format(x2, subtitles.VERTICAL_HOOK_TOP_Y) in lines[1]


def test_generate_vertical_hook_ass_alternates_anchor_per_shot():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": [], "emphasis": [], "style": "big", "caption": "今話題のAI"},
        {"out_start": 2.0, "out_end": 4.0, "lines": [], "emphasis": [], "style": "base", "caption": "悔しくて調べた"},
        {"out_start": 4.0, "out_end": 6.0, "lines": [], "emphasis": [], "style": "base", "caption": "続きは保存してね"},
    ]
    ass_text = subtitles.generate_vertical_hook_ass(pieces)
    assert "Style: VerticalHook,Noto Serif JP Black,{},".format(subtitles.VERTICAL_HOOK_FONT_SIZE) in ass_text
    # 1本目(偶数index=0)は右端、2本目(奇数index=1)は左端、3本目(偶数index=2)は右端。
    lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert "\\pos({},{})".format(subtitles.VERTICAL_HOOK_RIGHT_X, subtitles.VERTICAL_HOOK_TOP_Y) in lines[0]
    assert "\\pos({},{})".format(subtitles.VERTICAL_HOOK_LEFT_X, subtitles.VERTICAL_HOOK_TOP_Y) in lines[1]
    assert "\\pos({},{})".format(subtitles.VERTICAL_HOOK_RIGHT_X, subtitles.VERTICAL_HOOK_TOP_Y) in lines[2]


def test_generate_ass_with_style_delegates_to_vertical_hook_when_preset_set():
    pieces = [{"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"], "emphasis": [], "style": "base", "caption": "テスト"}]
    ass_text = subtitles.generate_ass_with_style(pieces, {"preset": "vertical_hook"})
    assert "VerticalHook" in ass_text
    assert "Style: Base," not in ass_text


def test_build_telop_pieces_from_shots_includes_raw_caption_field():
    shots = [{"id": "s1", "duration_sec": 2.0, "caption_jp": "今日から始めよう"}]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert pieces[0]["caption"] == "今日から始めよう"


# --- vertical_hookの「上品モダン+視認性最強」拡張: fad/列stagger/自動アクセント -----------

def test_build_vertical_hook_dialogue_lines_has_vertical_fad_tag():
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 3.0, "今話題のAI動画", anchor="right")
    assert len(lines) == 1
    assert "\\fad(140,100)" in lines[0]


def test_build_vertical_hook_dialogue_lines_second_column_start_delayed_by_stagger():
    """2列目以降のStartは1列につき+0.09秒ずつ遅らせる（列単位でリズムを作る演出）。"""
    text = "あいうえおかきくけこさし"  # 12文字 -> 2列
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 3.0, text, anchor="right")
    assert len(lines) == 2
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,")
    assert lines[1].startswith("Dialogue: 0,0:00:00.09,")


def test_build_vertical_hook_dialogue_lines_stagger_clamped_when_duration_is_very_short():
    """表示尺が極端に短いcaptionでは、2列目のStart遅延がout_end-0.3を超えないようクランプされる。

    out_start=0.0, out_end=0.35 のとき、素の遅延(0.09s)ではなく
    クランプ後の 0.35-0.3=0.05s が使われる。"""
    text = "あいうえおかきくけこさし"  # 12文字 -> 2列
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 0.35, text, anchor="right")
    assert len(lines) == 2
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,")
    assert lines[1].startswith("Dialogue: 0,0:00:00.05,")


def test_build_vertical_hook_dialogue_lines_applies_color_map_per_column_with_reset():
    """color_mapは列（別Dialogueイベント）ごとに\\cタグを張り直す必要がある（\\Nを跨いで持続
    するが列をまたいでは持続しないため）。長音「ー」置換後(｜)でも、置換前captionの
    span indexがそのまま使えることを確認する。"""
    caption = "結果ーすごい"
    accent_bgr = subtitles.hex_to_ass_bgr("#FFD84D")
    color_map = subtitles._color_map_from_spans(len(caption), [(0, 2)], accent_bgr)  # 「結果」に着色
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 2.0, caption, anchor="right", color_map=color_map)
    assert len(lines) == 1
    text = lines[0]
    assert "{{\\c{}}}結{{\\c{}}}".format(accent_bgr, subtitles._WHITE_RESET) in text
    assert "{{\\c{}}}果{{\\c{}}}".format(accent_bgr, subtitles._WHITE_RESET) in text
    assert "｜" in text  # 長音は置換後も残る(着色対象外=無色のまま)
    assert "{{\\c{}}}｜".format(accent_bgr) not in text  # ｜自体は着色されていない


def test_generate_vertical_hook_ass_reflects_custom_accent_color_from_subtitle_style():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": [], "emphasis": [], "style": "base", "caption": "3日間で変わる"},
    ]
    ass_text = subtitles.generate_vertical_hook_ass(pieces, subtitle_style={"accent_color": "#00FF00"})
    accent_bgr = subtitles.hex_to_ass_bgr("#00FF00")
    assert "\\c{}".format(accent_bgr) in ass_text


def test_generate_vertical_hook_ass_default_accent_color_is_ffd84d_when_unspecified():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": [], "emphasis": [], "style": "base", "caption": "3日間で変わる"},
    ]
    ass_text = subtitles.generate_vertical_hook_ass(pieces)
    default_bgr = subtitles.hex_to_ass_bgr(subtitles.DEFAULT_SUBTITLE_STYLE["accent_color"])
    assert "\\c{}".format(default_bgr) in ass_text


# --- animation_enabled（縦書きテロップのカットイン化: fad/列staggerを無効化） ---

def test_vertical_hook_dialogue_lines_animation_disabled_has_no_fad():
    lines = subtitles.build_vertical_hook_dialogue_lines(
        0.0, 3.0, "今話題のAI動画", anchor="right", animation_enabled=False
    )
    assert len(lines) == 1
    assert "\\fad(" not in lines[0]


def test_vertical_hook_dialogue_lines_animation_disabled_disables_column_stagger():
    """animation_enabled=False では2列目以降のStart遅延(stagger)を無効化し、全列out_startで開始する。"""
    text = "あいうえおかきくけこさし"  # 12文字 -> 2列
    lines = subtitles.build_vertical_hook_dialogue_lines(
        0.0, 3.0, text, anchor="right", animation_enabled=False
    )
    assert len(lines) == 2
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,")
    assert lines[1].startswith("Dialogue: 0,0:00:00.00,")  # staggerなし=1列目と同じStart


def test_vertical_hook_dialogue_lines_default_is_animated_backward_compatible():
    lines = subtitles.build_vertical_hook_dialogue_lines(0.0, 3.0, "今話題のAI動画", anchor="right")
    assert "\\fad(140,100)" in lines[0]


def test_generate_vertical_hook_ass_animation_disabled_has_no_fad_anywhere():
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": [], "emphasis": [], "style": "big", "caption": "今話題のAI"},
        {"out_start": 2.0, "out_end": 4.0, "lines": [], "emphasis": [], "style": "base", "caption": "悔しくて調べた"},
    ]
    ass_text = subtitles.generate_vertical_hook_ass(pieces, animation_enabled=False)
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    assert dialogue_lines
    for line in dialogue_lines:
        assert "\\fad(" not in line
    # \pos による配置指定自体は残る。
    assert "\\pos(" in ass_text
