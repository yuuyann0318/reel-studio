# -*- coding: utf-8 -*-
"""テロップ見た目の高解像度モデル `style_detail` と、その正規化・レガシー派生・幾何変換。

参考テロップの「フォント種別 / ウェイト / 塗り色 / 縁色・縁太さ / 背景(帯/箱) / 位置 /
サイズ / 行数」を1つの辞書 `style_detail` に集約する。vision で該当時刻フレームから抽出し、
fusion で telops[].style_detail に載せ、director→subtitles(ASS)→premiere へ貫通させる。

後方互換の柱:
- 旧 telop は `position`(top/mid/bottom) + `style`{color,stroke,size_class} の粗い3属性しか
  持たない。`derive_style_detail(tel)` は style_detail があればそれを、無ければ旧3属性から
  「それらしい」style_detail を合成する。よって旧キャッシュspecでも下流が動く。
- 不明値は font_class/weight/stroke_width/bg/pos_x では "unknown"、数値(pos_y_pct/size_pct/
  line_count)では None を使う（ハルシネーション回避: 「見えない」を "unknown/None" で表す）。

Python 3.9 互換構文のみ。stdlib のみ。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:
    from pipeline import font_map as _font_map
except Exception:  # pragma: no cover
    _font_map = None  # type: ignore

# ---------------------------------------------------------------------------
# 語彙
# ---------------------------------------------------------------------------
FONT_CLASSES = ("gothic", "mincho", "rounded", "pop", "handwriting")
WEIGHTS = ("normal", "bold", "heavy")
STROKE_WIDTHS = ("none", "thin", "thick")
BG_KINDS = ("none", "box", "band")
POS_X_VALUES = ("left", "center", "right")
UNKNOWN = "unknown"

# 色語 -> #RRGGBB（vision が hex を返せない場合の語彙フォールバック）。
COLOR_WORD_HEX = {
    "white": "#ffffff",
    "black": "#111111",
    "yellow": "#fff04d",
    "gold": "#f5c542",
    "pink": "#ff6ea7",
    "red": "#ff5555",
    "orange": "#ffa33a",
    "green": "#7ee07e",
    "blue": "#5eb0ff",
    "cyan": "#54e7f2",
    "purple": "#b58cff",
    "gray": "#9aa0a6",
    "grey": "#9aa0a6",
    "beige": "#f0e6d2",
    "brown": "#6b4a2a",
}

# size_class -> size_pct(画面高さに対する文字高%の概算)。Reels の体感に合わせた粗い値。
SIZE_CLASS_PCT = {"small": 3.2, "medium": 4.5, "med": 4.5, "large": 6.5, "xl": 8.5}
# 位置語 -> pos_y_pct(画面上端からの%)。
POSITION_POS_Y_PCT = {
    "top": 16.0, "upper": 16.0,
    "mid": 50.0, "middle": 50.0, "center": 50.0,
    "bottom": 82.0, "lower": 82.0,
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def normalize_hex(raw: Any) -> str:
    """"#rrggbb" / "rrggbb" / "0xRRGGBB" / 色語 を "#rrggbb"(小文字) へ。不明は ""。"""
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    if s in COLOR_WORD_HEX:
        return COLOR_WORD_HEX[s]
    if s.startswith("0x"):
        s = "#" + s[2:]
    if not s.startswith("#"):
        s = "#" + s
    if _HEX_RE.match(s):
        return s if s.startswith("#") else ("#" + s)
    # 色語が装飾付き（"black stroke" 等）で来た場合は含有色語を拾う。
    for word, hx in COLOR_WORD_HEX.items():
        if word in s:
            return hx
    return ""


def _enum(value: Any, allowed, default: str = UNKNOWN) -> str:
    s = (str(value).strip().lower() if value is not None else "")
    return s if s in allowed else default


def _num_or_none(value: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if v < lo:
        v = lo
    if v > hi:
        v = hi
    return round(v, 2)


def _int_or_none(value: Any, lo: int, hi: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = int(round(float(value)))
    return max(lo, min(hi, v))


def empty_style_detail() -> Dict[str, Any]:
    return {
        "font_class": UNKNOWN,
        "weight": UNKNOWN,
        "fill_color_hex": "",
        "stroke_color_hex": "",
        "stroke_width": UNKNOWN,
        "bg": UNKNOWN,
        "bg_color_hex": "",
        "pos_x": UNKNOWN,
        "pos_y_pct": None,
        "size_pct": None,
        "line_count": None,
    }


def normalize_style_detail(raw: Any) -> Dict[str, Any]:
    """任意の style_detail 入力を許容スキーマへ正規化する（未知/不正は unknown/None/""）。"""
    out = empty_style_detail()
    if not isinstance(raw, dict):
        return out
    fc = _enum(raw.get("font_class"), FONT_CLASSES, UNKNOWN)
    # font_map の別名吸収も通す（sans->gothic 等）。ただし unknown はそのまま残す。
    if fc == UNKNOWN and raw.get("font_class") not in (None, "", UNKNOWN) and _font_map is not None:
        fc = _font_map.normalize_font_class(raw.get("font_class"))
    out["font_class"] = fc
    out["weight"] = _enum(raw.get("weight"), WEIGHTS, UNKNOWN)
    out["fill_color_hex"] = normalize_hex(raw.get("fill_color_hex"))
    out["stroke_color_hex"] = normalize_hex(raw.get("stroke_color_hex"))
    out["stroke_width"] = _enum(raw.get("stroke_width"), STROKE_WIDTHS, UNKNOWN)
    out["bg"] = _enum(raw.get("bg"), BG_KINDS, UNKNOWN)
    out["bg_color_hex"] = normalize_hex(raw.get("bg_color_hex"))
    out["pos_x"] = _enum(raw.get("pos_x"), POS_X_VALUES, UNKNOWN)
    out["pos_y_pct"] = _num_or_none(raw.get("pos_y_pct"), 0.0, 100.0)
    out["size_pct"] = _num_or_none(raw.get("size_pct"), 0.5, 40.0)
    out["line_count"] = _int_or_none(raw.get("line_count"), 1, 8)
    return out


def _stroke_from_legacy(stroke_text: str) -> Dict[str, Any]:
    """旧 style.stroke の英語表現から (stroke_width, stroke_color_hex, bg, bg_color_hex) を推定。"""
    s = (stroke_text or "").strip().lower()
    res = {"stroke_width": UNKNOWN, "stroke_color_hex": "", "bg": UNKNOWN, "bg_color_hex": ""}
    if not s:
        return res
    if "no stroke" in s or s in ("none", "no", "no outline"):
        res["stroke_width"] = "none"
        res["bg"] = "none"
        return res
    # 背景帯/箱/ハイライトの検出。
    if "band" in s or "帯" in s or "bar" in s:
        res["bg"] = "band"
        res["bg_color_hex"] = normalize_hex(s)
    elif "box" in s or "highlight" in s or "背景" in s or "backdrop" in s:
        res["bg"] = "box"
        res["bg_color_hex"] = normalize_hex(s)
    # 縁取り/シャドウ/グロー。
    if "stroke" in s or "outline" in s or "縁" in s:
        res["stroke_width"] = "thick"
        res["stroke_color_hex"] = normalize_hex(s) or "#111111"
    elif "shadow" in s or "glow" in s or "影" in s:
        res["stroke_width"] = "thin"
        res["stroke_color_hex"] = normalize_hex(s) or "#111111"
    return res


def derive_style_detail(tel: Any) -> Dict[str, Any]:
    """telop(dict) から style_detail を得る。style_detail があれば正規化、無ければ旧3属性から合成。

    tel は参考 spec の telops 要素:
      {start,end,text, position, style:{color,stroke,size_class}, emphasis_words, style_detail?}
    """
    if not isinstance(tel, dict):
        return empty_style_detail()
    if isinstance(tel.get("style_detail"), dict):
        sd = normalize_style_detail(tel.get("style_detail"))
    else:
        sd = empty_style_detail()

    legacy_style = tel.get("style") if isinstance(tel.get("style"), dict) else {}

    # fill_color: style_detail 優先、無ければ旧 color 語。
    if not sd["fill_color_hex"]:
        sd["fill_color_hex"] = normalize_hex(legacy_style.get("color"))

    # stroke / bg: 旧 stroke 文から推定（style_detail が unknown のものだけ埋める）。
    legacy_stroke = _stroke_from_legacy(legacy_style.get("stroke") or "")
    if sd["stroke_width"] == UNKNOWN:
        sd["stroke_width"] = legacy_stroke["stroke_width"]
    if not sd["stroke_color_hex"]:
        sd["stroke_color_hex"] = legacy_stroke["stroke_color_hex"]
    if sd["bg"] == UNKNOWN:
        sd["bg"] = legacy_stroke["bg"]
    if not sd["bg_color_hex"]:
        sd["bg_color_hex"] = legacy_stroke["bg_color_hex"]

    # pos_y_pct: 無ければ position 語から。
    if sd["pos_y_pct"] is None:
        pos = (tel.get("position") or "").strip().lower()
        sd["pos_y_pct"] = POSITION_POS_Y_PCT.get(pos)

    # size_pct: 無ければ size_class から。
    if sd["size_pct"] is None:
        sc = (legacy_style.get("size_class") or "").strip().lower()
        sd["size_pct"] = SIZE_CLASS_PCT.get(sc)

    # line_count: 無ければ text の改行数から。
    if sd["line_count"] is None:
        text = tel.get("text")
        if isinstance(text, str) and text:
            sd["line_count"] = text.count("\n") + 1

    return sd


def is_effective(sd: Any) -> bool:
    """style_detail が「何かしら描画に効く情報」を持つか（全部 unknown/None/"" なら False）。"""
    if not isinstance(sd, dict):
        return False
    if sd.get("font_class") not in (None, "", UNKNOWN):
        return True
    if sd.get("weight") not in (None, "", UNKNOWN):
        return True
    for k in ("fill_color_hex", "stroke_color_hex", "bg_color_hex"):
        if sd.get(k):
            return True
    if sd.get("stroke_width") not in (None, "", UNKNOWN):
        return True
    if sd.get("bg") not in (None, "", UNKNOWN):
        return True
    if sd.get("pos_x") not in (None, "", UNKNOWN):
        return True
    for k in ("pos_y_pct", "size_pct", "line_count"):
        if sd.get(k) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# 幾何変換（ASS/Premiere 共通で使う）
# ---------------------------------------------------------------------------

# ★実測較正（2026-07-26・PIL計測 NotoSansJP-Black @ play_res_y=1920）:
#   旧実装は「グリフ高 ≈ 0.68 * fontsize」の Latin 向け経験則で逆算していたが、実際に
#   使用する日本語フォント(NotoSansJP-Black)の漢字グリフ bbox 高は fontsize の約 0.93 倍
#   （例: fontsize=120 → 単漢字bbox ≈ 112px ≈ 5.83%H）。0.68 のままだと size_pct を渡した
#   ときにフォントが約37%過大になり、size_pct が欠落した shot は既定 76px(=約3.7%H bbox・
#   実レンダ 2.2-2.8%H) まで縮み、参考(5.8-6.1%H)の半分以下のテロップになっていた
#   （実ペア第2弾診断 #2/#4）。実測比 0.90（headroom込み）で逆算し直し、size_pct 欠落時の
#   既定も参考の体感サイズ(≈5.8%H)に一致する 120px へ引き上げる。
_GLYPH_HEIGHT_RATIO = 0.90
# size_pct 欠落時の既定 font px（≈5.83%H bbox。参考テロップの実測 5.8-6.1%H に一致）。
DEFAULT_TELOP_FONT_PX = 120
# 上限クランプ（≈8.0%H bbox）。参考が特大フックでも2行×13字がセーフゾーンに収まるよう
# 過大化を止める。長文は subtitles 側の幅ラップ(wrap_caption_by_width)がさらに縮める。
_MAX_TELOP_FONT_PX = 160
_MIN_TELOP_FONT_PX = 44


def size_pct_to_font_px(size_pct: Any, play_res_y: int, fallback_px: int = DEFAULT_TELOP_FONT_PX) -> int:
    """size_pct(文字高%) -> ASS \\fs 相当の font px。

    グリフ bbox 高 ≈ _GLYPH_HEIGHT_RATIO * fontsize（NotoSansJP-Black 実測 0.93 に headroom
    を見て 0.90）で fontsize を逆算し、可読域へクランプ。size_pct が None のときは fallback_px。
    """
    if size_pct is None or not isinstance(size_pct, (int, float)) or isinstance(size_pct, bool):
        return int(fallback_px)
    glyph_px = float(size_pct) / 100.0 * float(play_res_y)
    fs = glyph_px / _GLYPH_HEIGHT_RATIO
    return int(max(_MIN_TELOP_FONT_PX, min(_MAX_TELOP_FONT_PX, round(fs))))


def pos_y_pct_to_y(pos_y_pct: Any, play_res_y: int, margin_pct: float = 8.0) -> Optional[int]:
    """pos_y_pct(上端からの%) -> \\pos の y 座標(px)。None は None。セーフゾーンへクランプ。"""
    if pos_y_pct is None or not isinstance(pos_y_pct, (int, float)) or isinstance(pos_y_pct, bool):
        return None
    y = float(pos_y_pct) / 100.0 * float(play_res_y)
    lo = margin_pct / 100.0 * float(play_res_y)
    hi = float(play_res_y) - lo
    return int(max(lo, min(hi, round(y))))


def pos_x_to_x(pos_x: Any, play_res_x: int) -> Optional[int]:
    """pos_x(left/center/right) -> \\pos の x 座標(px, \\an5 中央アンカー前提)。unknown は None。"""
    s = (str(pos_x).strip().lower() if pos_x is not None else "")
    if s == "left":
        return int(round(0.30 * play_res_x))
    if s == "right":
        return int(round(0.70 * play_res_x))
    if s == "center":
        return int(round(0.50 * play_res_x))
    return None
