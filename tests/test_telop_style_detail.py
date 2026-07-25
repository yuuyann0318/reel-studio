# -*- coding: utf-8 -*-
"""style_detail(テロップ見た目高解像度モデル)・font_map・ASS/Premiere写像の単体テスト。

参考テロップの「フォント/色/サイズ/位置/縁/背景」を忠実再現する経路を検証する。
"""
import os

from pipeline import font_map, telop_style
from pipeline import subtitles as S
from premiere import export_xmeml as X


# ---------------------------------------------------------------------------
# font_map: 実在フォントのみ返す
# ---------------------------------------------------------------------------

def test_font_map_returns_only_existing_files():
    avail = font_map.available_jp_fonts()
    for cls, info in avail.items():
        # exists=True のフォントは assets/fonts に実ファイルが在ること。
        if info["exists"]:
            assert os.path.exists(os.path.join(font_map.fonts_dir(), info["file"])), cls


def test_font_map_all_five_classes_present_on_this_repo():
    avail = font_map.available_jp_fonts()
    for cls in ("gothic", "mincho", "rounded", "pop", "handwriting"):
        assert avail[cls]["exists"], "同梱フォントが見つからない: {}".format(cls)


def test_resolve_font_maps_classes_to_expected_families():
    assert font_map.resolve_font("gothic")[0] == "Noto Sans JP Black"
    assert font_map.resolve_font("mincho")[0] == "Noto Serif JP Black"
    assert font_map.resolve_font("rounded")[0] == "Zen Maru Gothic Black"
    assert font_map.resolve_font("pop")[0] == "Mochiy Pop One"
    assert font_map.resolve_font("handwriting")[0] == "Klee One SemiBold"


def test_resolve_font_unknown_falls_back_to_gothic():
    fam, bold, warns = font_map.resolve_font("unknown")
    assert fam == "Noto Sans JP Black"


def test_resolve_font_weight_bold_flag():
    assert font_map.resolve_font("gothic", "heavy")[1] == -1
    assert font_map.resolve_font("gothic", "bold")[1] == -1
    assert font_map.resolve_font("gothic", "normal")[1] == 0


# ---------------------------------------------------------------------------
# telop_style: 正規化・派生
# ---------------------------------------------------------------------------

def test_normalize_style_detail_unknowns():
    sd = telop_style.normalize_style_detail({})
    assert sd["font_class"] == "unknown"
    assert sd["fill_color_hex"] == ""
    assert sd["pos_y_pct"] is None
    assert sd["line_count"] is None


def test_normalize_style_detail_clamps_and_hex():
    sd = telop_style.normalize_style_detail({
        "font_class": "gothic", "weight": "heavy", "fill_color_hex": "FF00AA",
        "stroke_width": "thick", "bg": "band", "pos_x": "right",
        "pos_y_pct": 250.0, "size_pct": 7.0, "line_count": 3,
    })
    assert sd["fill_color_hex"] == "#ff00aa"
    assert sd["pos_y_pct"] == 100.0  # clamp
    assert sd["bg"] == "band"
    assert sd["line_count"] == 3


def test_derive_style_detail_from_legacy_only():
    tel = {
        "text": "汚肌改善\n生活", "position": "mid",
        "style": {"color": "white", "stroke": "black stroke", "size_class": "large"},
    }
    sd = telop_style.derive_style_detail(tel)
    assert sd["fill_color_hex"] == "#ffffff"
    assert sd["stroke_width"] == "thick"
    assert sd["pos_y_pct"] == telop_style.POSITION_POS_Y_PCT["mid"]
    assert sd["size_pct"] == telop_style.SIZE_CLASS_PCT["large"]
    assert sd["line_count"] == 2  # 改行1つ→2行


def test_derive_style_detail_prefers_explicit():
    tel = {
        "text": "x", "position": "top",
        "style": {"color": "white"},
        "style_detail": {"font_class": "mincho", "fill_color_hex": "#ff6ea7", "pos_y_pct": 12.0},
    }
    sd = telop_style.derive_style_detail(tel)
    assert sd["font_class"] == "mincho"
    assert sd["fill_color_hex"] == "#ff6ea7"
    assert sd["pos_y_pct"] == 12.0


def test_geometry_helpers_monotonic():
    y_top = telop_style.pos_y_pct_to_y(15.0, 1920)
    y_bot = telop_style.pos_y_pct_to_y(85.0, 1920)
    assert y_top < y_bot
    assert telop_style.pos_y_pct_to_y(None, 1920) is None
    f_small = telop_style.size_pct_to_font_px(3.0, 1920)
    f_large = telop_style.size_pct_to_font_px(8.0, 1920)
    assert f_small < f_large


# ---------------------------------------------------------------------------
# subtitles: style_detail → ASS override
# ---------------------------------------------------------------------------

def _hint(sd):
    return {"style_detail": sd}


def test_ass_override_emits_font_color_stroke_position():
    sd = telop_style.normalize_style_detail({
        "font_class": "mincho", "weight": "bold", "fill_color_hex": "#ff6ea7",
        "stroke_color_hex": "#cc1493", "stroke_width": "thin", "pos_x": "center", "pos_y_pct": 18.0,
    })
    ov = S.build_hint_override_prefix(_hint(sd))
    assert "\\fnNoto Serif JP Black" in ov
    assert "\\b1" in ov
    assert "\\1c" in ov       # fill
    assert "\\3c" in ov       # stroke color
    assert "\\bord" in ov
    assert "\\pos(" in ov     # 位置


def test_ass_font_px_from_size_pct():
    sd = telop_style.normalize_style_detail({"size_pct": 8.0})
    px = S.hint_font_px(_hint(sd), 76)
    assert px > 76  # 大きめ


def test_bg_band_emits_drawing_rect():
    sd = telop_style.normalize_style_detail({"bg": "band", "bg_color_hex": "#222222", "pos_y_pct": 20.0})
    line = S.build_bg_rect_dialogue(0.0, 2.0, ["帯背景"], _hint(sd), 80)
    assert line is not None
    assert "\\p1" in line and "\\1c" in line
    # bg なしは None
    sd2 = telop_style.normalize_style_detail({"bg": "none"})
    assert S.build_bg_rect_dialogue(0.0, 2.0, ["x"], _hint(sd2), 80) is None


def test_generate_ass_includes_bg_line_before_text():
    shots = [{
        "id": "s1", "duration_sec": 3.0, "caption_jp": "テスト",
        "telop_style_hint": {"style_detail": telop_style.normalize_style_detail(
            {"font_class": "gothic", "fill_color_hex": "#ffffff", "bg": "band",
             "bg_color_hex": "#111111", "pos_y_pct": 20.0, "size_pct": 6.0})},
    }]
    pieces = S.build_telop_pieces_from_shots(shots)
    ass = S.generate_ass(pieces)
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert len(dialogues) == 2  # bg rect + text
    assert "\\p1" in dialogues[0]           # 背景が先
    assert "\\fnNoto Sans JP Black" in dialogues[1]


def test_legacy_hint_still_works_without_style_detail():
    # style_detail 無しの粗いヒントは従来の \an/\c 経路。
    ov = S.build_hint_override_prefix({"position": "top", "color": "yellow"})
    assert ov is not None
    assert "\\an8" in ov and "\\c" in ov


# ---------------------------------------------------------------------------
# premiere: style_detail → xmeml パラメータ
# ---------------------------------------------------------------------------

def test_premiere_resolve_hint_uses_style_detail():
    hint = {"style_detail": telop_style.normalize_style_detail(
        {"font_class": "rounded", "fill_color_hex": "#ff6ea7", "pos_y_pct": 12.0, "size_pct": 7.0})}
    size, position, color_hex, font = X._resolve_hint_for_xmeml(hint, 60, "bottom_safe")
    assert font == "Zen Maru Gothic Black"
    assert color_hex == "#FF6EA7"
    assert position == X._HINT_POSITION_TO_PREMIERE.get("top", "top_safe")
    assert size > 24


def test_premiere_resolve_hint_legacy_fallback():
    size, position, color_hex, font = X._resolve_hint_for_xmeml(
        {"position": "top", "color": "white", "size_class": "large"}, 60, "bottom_safe")
    assert font == X.CAPTION_FONT_DEFAULT
    assert color_hex == X._HINT_COLOR_TO_HEX["white"]


# ---------------------------------------------------------------------------
# reference_v2: vision merge + validate 正規化
# ---------------------------------------------------------------------------

def test_reference_v2_vision_merges_style_detail():
    from pipeline import reference_v2

    def _fake_call(prompt, paths, timeout_sec=None):
        return {"ok": True, "data": [{
            "index": 1, "telop_text": "汚肌改善生活",
            "telop_position": "mid", "telop_color": "white",
            "telop_style_detail": {
                "font_class": "gothic", "weight": "heavy", "fill_color_hex": "#ffffff",
                "stroke_color_hex": "#111111", "stroke_width": "thick", "bg": "none",
                "pos_x": "center", "pos_y_pct": 18.0, "size_pct": 7.0, "line_count": 2,
            },
        }]}

    frames = [{"index": 1, "time": 0.5, "path": "/tmp/f1.png"}]
    results, warnings = reference_v2.analyze_frames_with_vision(
        frames, cfg={"reference": {"vision_batch_size": 1, "max_vision_calls": 1}},
        vision_call=_fake_call,
    )
    assert results and "telop_style_detail" in results[0]
    sd = results[0]["telop_style_detail"]
    assert sd["font_class"] == "gothic"
    assert sd["fill_color_hex"] == "#ffffff"


def test_reference_v2_vision_no_style_detail_when_no_text():
    from pipeline import reference_v2

    def _fake_call(prompt, paths, timeout_sec=None):
        return {"ok": True, "data": [{
            "index": 1, "telop_text": "",
            "telop_style_detail": {"font_class": "gothic"},
        }]}

    frames = [{"index": 1, "time": 0.5, "path": "/tmp/f1.png"}]
    results, _ = reference_v2.analyze_frames_with_vision(
        frames, cfg={"reference": {"vision_batch_size": 1, "max_vision_calls": 1}},
        vision_call=_fake_call,
    )
    # テロップ文言が無ければ style_detail は載せない（ハルシネーション回避）。
    assert "telop_style_detail" not in results[0]


def test_validate_reference_spec_normalizes_telop_style_detail():
    from pipeline import reference_v2
    spec = {
        "version": 2, "url": "u", "duration_sec": 10.0, "transcript": "",
        "segments": [], "beats": [], "rhythm": {}, "cuts": [],
        "shots_ref": [], "sfx_events": [], "bgm": {}, "warnings": [],
        "telops": [{"start": 0.0, "end": 2.0, "text": "x", "position": "mid",
                    "style": {"color": "white"},
                    "style_detail": {"font_class": "gothic", "fill_color_hex": "FF00AA",
                                     "pos_y_pct": 999}}],
    }
    ok, errors, normalized = reference_v2.validate_reference_spec_v2(spec)
    assert ok, errors
    sd = normalized["telops"][0]["style_detail"]
    assert sd["fill_color_hex"] == "#ff00aa"
    assert sd["pos_y_pct"] == 100.0  # clamp


def test_validate_reference_spec_rejects_bad_style_detail():
    from pipeline import reference_v2
    spec = {
        "version": 2, "url": "u", "duration_sec": 10.0, "transcript": "",
        "segments": [], "beats": [], "rhythm": {}, "cuts": [],
        "shots_ref": [], "sfx_events": [], "bgm": {}, "warnings": [],
        "telops": [{"start": 0.0, "end": 2.0, "text": "x", "style_detail": "notadict"}],
    }
    ok, errors, _ = reference_v2.validate_reference_spec_v2(spec)
    assert not ok


# ---------------------------------------------------------------------------
# director: telops → shot.telop_style_hint.style_detail 貫通
# ---------------------------------------------------------------------------

def test_director_maps_style_detail_into_hint():
    from pipeline import director
    telops = [{"start": 0.0, "end": 2.0, "text": "汚肌改善", "position": "mid",
               "style": {"color": "white", "stroke": "black stroke", "size_class": "large"}}]
    boundaries = [0.0, 3.0]
    out = director._map_telops_to_shots(telops, boundaries, ["s1"], [3.0], 3.0)
    hint = out["s1"]["telop_style_hint"]
    assert "style_detail" in hint
    assert hint["style_detail"]["fill_color_hex"] == "#ffffff"


# ---------------------------------------------------------------------------
# fidelity: 5属性 style_detail 一致率
# ---------------------------------------------------------------------------

def test_fidelity_style5_perfect_match():
    from qa import fidelity
    spec = {
        "duration_sec": 3.0,
        "telops": [{"start": 0.0, "end": 3.0, "text": "汚肌改善", "position": "mid",
                    "style": {"color": "white", "stroke": "black stroke", "size_class": "large"}}],
    }
    # 参考と同じ style_detail を持つ plan（director 派生と一致する想定）。
    sd = telop_style.derive_style_detail(spec["telops"][0])
    plan = {
        "shots": [{"id": "s1", "duration_sec": 3.0, "caption_jp": "汚肌改善",
                   "telop_style_hint": {"style_detail": sd}}],
    }
    res = fidelity._telop_style_match(spec, plan)
    assert res["score"] >= 0.99, res


def test_fidelity_style5_font_mismatch_lowers_score():
    from qa import fidelity
    spec = {
        "duration_sec": 3.0,
        "telops": [{"start": 0.0, "end": 3.0, "text": "x", "position": "mid",
                    "style_detail": {"font_class": "mincho", "fill_color_hex": "#ffffff",
                                     "stroke_width": "thick", "pos_y_pct": 50.0, "size_pct": 6.5}}],
    }
    plan = {
        "shots": [{"id": "s1", "duration_sec": 3.0, "caption_jp": "x",
                   "telop_style_hint": {"style_detail": {
                       "font_class": "pop", "fill_color_hex": "#ffffff",
                       "stroke_width": "thick", "pos_y_pct": 50.0, "size_pct": 6.5}}}],
    }
    res = fidelity._telop_style_match(spec, plan)
    assert res["score"] < 1.0
