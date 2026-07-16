# -*- coding: utf-8 -*-
"""ASS字幕生成。PlayRes 1080x1920、Style Base/Big。

video-auto-editor/pipeline/subtitles.py のASS生成コア（ヘッダ/ダイアログ組み立て・
色タグ/特殊文字エスケープ）をそのまま移植し、reel向けに「shots配列（各ショット=
1キャプション、ショットの表示区間に同期）からtelop断片を組み立てる」ヘルパーを追加した。

DESIGN原則を踏襲: PlayRes=1080x1920, ASSはBGR順(`\\c&HBBGGRR&`)。
Python 3.9 互換構文のみ。
"""
from __future__ import annotations

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

STYLE_BASE_FONTSIZE = 76
STYLE_BIG_FONTSIZE = 92
MARGIN_L = 60
MARGIN_R = 60
MARGIN_V = 420
OUTLINE = 6
SHADOW = 2
ALIGNMENT = 2  # bottom-center

_WHITE_RESET = "&HFFFFFF&"

_ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,Noto Sans JP Black,{base_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1
Style: Big,Noto Sans JP Black,{big_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

FORBIDDEN_LINE_START = "」』）。、！？ゃゅょっー"

# 簡易文節分割用: これらの文字列で終わる位置は「文節/句読点の区切り」とみなし、
# 改行位置として優先する（語中分割を避けるための簡易ヒューリスティック）。
# 長い語尾から先に判定するよう長さ降順に並べる。
_PARTICLE_BREAK_ENDINGS = tuple(
    sorted(
        [
            "ですが", "ますが", "けれども", "けれど", "ながら", "でも",
            "には", "とは", "から", "まで", "より", "ので", "のに",
            "として", "については", "によって",
            "は", "が", "を", "に", "で", "と", "も", "の", "へ", "や",
            "、", "。", "！", "？", "!", "?",
        ],
        key=len,
        reverse=True,
    )
)


def hex_to_ass_bgr(hex_color):
    """`#RRGGBB` を ASS の `&HBBGGRR&` 形式へ変換する。"""
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        raise ValueError("hex_color は #RRGGBB 形式である必要があります: {!r}".format(hex_color))
    r, g, b = h[0:2], h[2:4], h[4:6]
    return "&H{}{}{}&".format(b.upper(), g.upper(), r.upper())


def seconds_to_ass_time(t):
    if t is None or t < 0:
        t = 0.0
    total_cs = int(round(t * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return "{:d}:{:02d}:{:02d}.{:02d}".format(h, m, s, cs)


_ASS_BACKSLASH_GUARD = "\x00__ASS_BS_GUARD__\x00"


def escape_ass_text(text):
    """ASS Dialogue Textのプレーン文字部分として安全な形にエスケープする。"""
    if not text:
        return text
    text = text.replace("\\", _ASS_BACKSLASH_GUARD)
    text = text.replace("{", "｛").replace("}", "｝")
    text = text.replace(_ASS_BACKSLASH_GUARD, "\\{}")
    return text


def _compute_color_map(text, emphasis):
    color_at = [None] * len(text)
    for em in emphasis or []:
        em_text = em.get("text")
        color = em.get("color")
        if not em_text or not color:
            continue
        bgr = hex_to_ass_bgr(color)
        start = 0
        while True:
            idx = text.find(em_text, start)
            if idx < 0:
                break
            end = idx + len(em_text)
            for i in range(idx, end):
                if color_at[i] is None:
                    color_at[i] = bgr
            start = end
    return color_at


def _render_with_color_map(text, color_at):
    parts = []
    i, n = 0, len(text)
    while i < n:
        c = color_at[i]
        j = i
        while j < n and color_at[j] == c:
            j += 1
        chunk = escape_ass_text(text[i:j])
        if c is None:
            parts.append(chunk)
        else:
            parts.append("{{\\c{}}}{}{{\\c{}}}".format(c, chunk, _WHITE_RESET))
        i = j
    return "".join(parts)


def build_dialogue_text(lines, emphasis=None):
    joined = "".join(lines)
    color_at = _compute_color_map(joined, emphasis)
    rendered_lines = []
    cursor = 0
    for line in lines:
        segment_colors = color_at[cursor:cursor + len(line)]
        rendered_lines.append(_render_with_color_map(line, segment_colors))
        cursor += len(line)
    return "\\N".join(rendered_lines)


def build_ass_header():
    return _ASS_HEADER_TEMPLATE.format(
        play_res_x=PLAY_RES_X,
        play_res_y=PLAY_RES_Y,
        base_size=STYLE_BASE_FONTSIZE,
        big_size=STYLE_BIG_FONTSIZE,
        outline=OUTLINE,
        shadow=SHADOW,
        alignment=ALIGNMENT,
        margin_l=MARGIN_L,
        margin_r=MARGIN_R,
        margin_v=MARGIN_V,
    )


def build_dialogue_line(out_start, out_end, lines, emphasis=None, style="base"):
    style_name = "Big" if style == "big" else "Base"
    start = seconds_to_ass_time(out_start)
    end = seconds_to_ass_time(out_end)
    text = build_dialogue_text(lines, emphasis)
    return "Dialogue: 0,{},{},{},,0,0,0,,{}".format(start, end, style_name, text)


def generate_ass(telop_pieces):
    """出力時刻マップ済みのtelop断片群からASS全文を生成する。

    telop_pieces: [{"out_start","out_end","lines","emphasis","style"}, ...]
    """
    lines_out = [build_ass_header().rstrip("\n")]
    for piece in sorted(telop_pieces, key=lambda p: p["out_start"]):
        lines_out.append(
            build_dialogue_line(
                piece["out_start"], piece["out_end"], piece["lines"], piece.get("emphasis"), piece.get("style", "base")
            )
        )
    return "\n".join(lines_out) + "\n"


# ---------------------------------------------------------------------------
# reel固有: shots配列 → telop断片（各ショットの表示区間＝ナレーションの対応区間に同期）
# ---------------------------------------------------------------------------

def _find_bunsetsu_break(text, max_chars):
    """text内でmax_chars以内に収まる最も右側の文節/句読点境界を探す（簡易文節分割）。

    見つからなければNoneを返す（呼び出し側は文字数ベースの禁則改行にフォールバックする）。
    """
    limit = min(max_chars, len(text) - 1)
    for i in range(limit, 0, -1):
        if text[i] in FORBIDDEN_LINE_START:
            continue
        for ending in _PARTICLE_BREAK_ENDINGS:
            if text[:i].endswith(ending):
                return i
    return None


def wrap_caption_kinsoku(text, max_chars=13, max_lines=2):
    """caption_jpを最大max_lines行に分割する。

    まず文節/句読点の境界（簡易分割）で13字以内に収まる改行位置を探し、見つかれば
    そこで折る（「リセ／ット」のような語中分割を避ける）。見つからない場合のみ、
    従来どおり13字禁則改行（行頭禁則文字は前行へ繰り込み）にフォールバックする。
    """
    text = (text or "").strip()
    if not text:
        return []
    lines = []
    remaining = text
    while remaining and len(lines) < max_lines - 1:
        cut = _find_bunsetsu_break(remaining, max_chars)
        if cut is None:
            cut = min(max_chars, len(remaining))
            while cut < len(remaining) and remaining[cut] in FORBIDDEN_LINE_START:
                cut += 1
        lines.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        lines.append(remaining)
    return lines


def build_telop_pieces_from_shots(shots, hook_shot_id=None):
    """shots（各ショットにduration_sec/caption_jpを持つ）から、ショット表示区間に
    同期したtelop断片列を組み立てる。

    ショットの累積時間で out_start/out_end を決める（=render.py がクリップを
    順番に連結する前提と一致させる）。hook_shot_id と一致するショットは
    style="big" にして強調する（最初のショットが無指定ならデフォルトで強調）。
    """
    pieces = []
    cursor = 0.0
    first_id = shots[0]["id"] if shots else None
    for shot in shots or []:
        dur = float(shot.get("duration_sec", 0.0))
        out_start, out_end = cursor, cursor + dur
        cursor = out_end
        caption = (shot.get("caption_jp") or "").strip()
        if not caption:
            continue
        lines = wrap_caption_kinsoku(caption, max_chars=13, max_lines=2)
        if not lines:
            continue
        style = "big" if shot.get("id") == (hook_shot_id or first_id) else "base"
        pieces.append({"out_start": out_start, "out_end": out_end, "lines": lines, "emphasis": [], "style": style})
    return pieces
