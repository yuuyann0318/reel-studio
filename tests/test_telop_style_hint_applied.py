# -*- coding: utf-8 -*-
"""F9: telop_style_hint が ASS 描画と xmeml generatoritem に反映されることの単体テスト。

背景:
  監査 F9 の要点は「skeleton は telop_style_hint (position/color/size_class) を生成
  しているが、subtitles.build_telop_pieces_from_shots / generate_ass_with_style は
  一切参照していない」ことだった。ここではその配線が実際に描画側へ到達している
  ことを機械的に検証する（実描画は行わず、生成 ASS/xmeml 文字列を検査する）。
"""
from __future__ import annotations

from pipeline import subtitles
from premiere import export_xmeml


def _make_plan_with_hint(position, color, size_class, sid="s1"):
    return {
        "id": sid,
        "duration_sec": 3.0,
        "caption_jp": "テストのキャプション",
        "telop_style_hint": {
            "position": position,
            "color": color,
            "size_class": size_class,
            "stroke": "black stroke",
            "emphasis_words": [],
        },
    }


# ---------------------------------------------------------------------------
# 1. build_telop_pieces_from_shots が piece に telop_style_hint を伝播する
# ---------------------------------------------------------------------------

def test_pieces_carry_telop_style_hint_from_shot():
    shots = [_make_plan_with_hint("top", "yellow", "large")]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 1
    hint = pieces[0].get("telop_style_hint")
    assert isinstance(hint, dict)
    assert hint["position"] == "top"
    assert hint["color"] == "yellow"
    assert hint["size_class"] == "large"


def test_pieces_do_not_carry_hint_when_shot_lacks_it():
    shots = [{"id": "s1", "duration_sec": 2.0, "caption_jp": "テスト"}]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert "telop_style_hint" not in pieces[0]


# ---------------------------------------------------------------------------
# 2. generate_ass / generate_ass_with_style が Dialogue に \an と \c を焼き込む
# ---------------------------------------------------------------------------

def test_generate_ass_applies_position_and_color_overrides_from_hint():
    shots = [_make_plan_with_hint("top", "yellow", "medium")]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    ass_text = subtitles.generate_ass(pieces)
    # \an8（top-center）と \c&H4DF0FF&（yellow #FFF04D の BGR）が Dialogue 行に入っていること
    dialogue_lines = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
    assert dialogue_lines, "少なくとも1件のDialogueが生成されていること"
    joined = "\n".join(dialogue_lines)
    assert "\\an8" in joined, "position=top → \\an8 が Dialogue に焼かれていない"
    assert "\\c&H4DF0FF&" in joined, "color=yellow → \\c<yellow BGR> が Dialogue に焼かれていない"


def test_generate_ass_with_style_applies_hint_when_available():
    shots = [_make_plan_with_hint("bottom", "pink", "small")]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    ass_text = subtitles.generate_ass_with_style(pieces)
    dialogue_lines = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
    joined = "\n".join(dialogue_lines)
    # bottom は既定 alignment=2 と同じなので \an は付かなくてよい。color=pink は焼かれる。
    assert "\\c&HA76EFF&" in joined, "color=pink → \\c<pink BGR> が Dialogue に焼かれていない"
    # size_class=small なので基準フォントより小さい \fs<n> が入る想定（wrap_caption_by_width
    # が hint 適用後の base で計算する）。少なくとも \fs<xx> の形跡があること
    assert "\\fs" in joined


def test_generate_ass_does_not_break_when_hint_absent():
    shots = [{"id": "s1", "duration_sec": 2.5, "caption_jp": "テスト"}]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    ass_text = subtitles.generate_ass(pieces)
    # hint が無い場合、\an や \c の override は挿入されない（既定 Style がそのまま使われる）。
    dialogue_lines = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
    assert dialogue_lines
    joined = "\n".join(dialogue_lines)
    assert "\\an" not in joined
    assert "\\c&H" not in joined  # 自動アクセントが入らない captions は色 override なし


# ---------------------------------------------------------------------------
# 3. xmeml generatoritem に size/position/fontcolor が反映される
# ---------------------------------------------------------------------------

def test_xmeml_generatoritem_reflects_telop_style_hint():
    hint = {"position": "top", "color": "yellow", "size_class": "large"}
    # F-STYLE: 返り値は (size, position, color_hex, font) の4-tuple（style_detail 対応で font 追加）。
    size, position, color_hex, font = export_xmeml._resolve_hint_for_xmeml(hint, base_size=60, base_position="bottom_safe")
    assert size == 78  # large -> 78
    assert position == "top_safe"
    assert color_hex == "#FFF04D"
    assert font == export_xmeml.CAPTION_FONT_DEFAULT  # legacy hint はデフォルトフォント


def test_xmeml_hint_resolver_falls_back_when_no_hint():
    size, position, color_hex, font = export_xmeml._resolve_hint_for_xmeml(None, base_size=60, base_position="bottom_safe")
    assert size == 60
    assert position == "bottom_safe"
    assert color_hex is None
    assert font == export_xmeml.CAPTION_FONT_DEFAULT


def test_hint_color_survives_auto_accent_reset():
    """codex-review P1 回帰: hint.color=yellow のときは、自動アクセント span の直後の
    `\\c` 戻し先が hint 色（yellow BGR）になるべき。旧実装では常に白リセットで hint 色が
    破壊されていた。
    """
    # 数字+単位を含む caption で自動アクセントを発生させる（"5秒" が着色される）
    shots = [{
        "id": "s1",
        "duration_sec": 3.0,
        "caption_jp": "たった5秒で完成する",
        "telop_style_hint": {
            "position": "top",
            "color": "yellow",
            "size_class": "medium",
        },
    }]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    # generate_ass_with_style は自動アクセント（product_name=None でも数字+単位を検出）を発火させる
    ass = subtitles.generate_ass_with_style(pieces)
    dialogue_lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert dialogue_lines
    joined = "\n".join(dialogue_lines)
    # accent span の後の \c 戻し先は白（&HFFFFFF&）ではなく yellow の BGR
    yellow_bgr = subtitles.hex_to_ass_bgr("#FFF04D")  # "&H4DF0FF&"
    # 具体的なパターン: 数字+単位spanの直後に \c<yellow BGR> が付く
    # e.g. "{\\c&H<accent BGR>}5秒{\\c&H4DF0FF&}"
    assert yellow_bgr in joined, "hint color=yellow が accent reset に反映されていない (got: {})".format(joined)


def test_hint_color_absent_uses_white_reset():
    """hint が無ければ従来どおり白リセット（後方互換）。"""
    shots = [{
        "id": "s1",
        "duration_sec": 3.0,
        "caption_jp": "たった5秒で完成する",
    }]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    ass = subtitles.generate_ass_with_style(pieces)
    dialogue_lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    joined = "\n".join(dialogue_lines)
    assert "&HFFFFFF&" in joined, "hint 無しでは白リセット（&HFFFFFF&）が使われるべき"


def test_xmeml_full_generatoritem_contains_fontcolor_when_hint_yellow():
    lines = export_xmeml._generatoritem_text_lines(
        gen_id="gen-test", name="TL01",
        start_frames=0, end_frames=60, in_frame=0, out_frame=60,
        timebase=30, ntsc="FALSE",
        text="テスト", wrapped_lines=["テスト"], depth=0,
        color_hex="#FFF04D",
    )
    xml = "\n".join(lines)
    assert "<parameterid>fontcolor</parameterid>" in xml
    assert "#FFF04D" in xml
