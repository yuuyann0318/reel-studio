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

# 句読点系の語尾（この後はひらがなが続いても改行境界として常に有効）。
_PUNCTUATION_BREAK_ENDINGS = frozenset(["、", "。", "！", "？", "!", "?"])


def _is_hiragana(ch):
    return "ぁ" <= ch <= "ゟ"


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


# ---------------------------------------------------------------------------
# Studio: plan.subtitle_style（font_size/accent_color/position）を反映したASS再生成
# ---------------------------------------------------------------------------

DEFAULT_SUBTITLE_STYLE = {"font_size": 76, "accent_color": "#FFD84D", "position": "lower", "preset": "default"}

SUBTITLE_STYLE_PRESETS = ("default", "vertical_hook")

# Alignment=2(bottom-center)基準でのMarginV（画面下端からの距離）。
_POSITION_MARGIN_V = {"lower": 420, "center": 900, "upper": 1400}

_STYLED_ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,Noto Sans JP Black,{base_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1
Style: Big,Noto Sans JP Black,{big_size},{big_primary},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _hex_to_ass_style_color(hex_color, alpha_hex="00"):
    """`#RRGGBB` を Style行のPrimaryColour用 `&HAABBGGRR` 形式へ変換する（alpha_hex省略時は不透明）。"""
    bgr_tag = hex_to_ass_bgr(hex_color)  # "&HBBGGRR&"
    bgr = bgr_tag[2:-1]
    return "&H{}{}".format(alpha_hex, bgr)


def resolve_subtitle_style(subtitle_style=None):
    """plan.subtitle_style（部分指定可）を DEFAULT_SUBTITLE_STYLE で補完して返す。

    preset は SUBTITLE_STYLE_PRESETS のいずれかに正規化する（未知の値/未指定は"default"）。
    """
    merged = dict(DEFAULT_SUBTITLE_STYLE)
    if isinstance(subtitle_style, dict):
        for k, v in subtitle_style.items():
            if v is not None:
                merged[k] = v
    if merged.get("preset") not in SUBTITLE_STYLE_PRESETS:
        merged["preset"] = "default"
    return merged


def build_ass_header_with_style(subtitle_style=None):
    """subtitle_style（font_size/accent_color/position）を反映したASSヘッダを構築する。

    Studio専用のテロップ再生成で使う（既存の build_ass_header()/generate_ass() は
    run.py のCLI経路向けとして変更せず残す）。DEFAULT_SUBTITLE_STYLE を既定値とし、
    Big(フック)スタイルの文字色に accent_color を反映する。
    """
    style = resolve_subtitle_style(subtitle_style)
    base_size = int(style.get("font_size") or STYLE_BASE_FONTSIZE)
    big_size = base_size + (STYLE_BIG_FONTSIZE - STYLE_BASE_FONTSIZE)
    margin_v = _POSITION_MARGIN_V.get(style.get("position"), MARGIN_V)
    accent_color = style.get("accent_color")
    big_primary = _hex_to_ass_style_color(accent_color) if accent_color else "&H00FFFFFF"

    return _STYLED_ASS_HEADER_TEMPLATE.format(
        play_res_x=PLAY_RES_X,
        play_res_y=PLAY_RES_Y,
        base_size=base_size,
        big_size=big_size,
        big_primary=big_primary,
        outline=OUTLINE,
        shadow=SHADOW,
        alignment=ALIGNMENT,
        margin_l=MARGIN_L,
        margin_r=MARGIN_R,
        margin_v=margin_v,
    )


def generate_ass_with_style(telop_pieces, subtitle_style=None):
    """generate_ass() のsubtitle_style対応版。Studioのテロップ再生成で使う。

    subtitle_style.preset=="vertical_hook" のときは横書き(Base/Big)ではなく、
    generate_vertical_hook_ass() による縦書きテロップを生成する。
    """
    style = resolve_subtitle_style(subtitle_style)
    if style.get("preset") == "vertical_hook":
        return generate_vertical_hook_ass(telop_pieces)

    lines_out = [build_ass_header_with_style(subtitle_style).rstrip("\n")]
    for piece in sorted(telop_pieces, key=lambda p: p["out_start"]):
        lines_out.append(
            build_dialogue_line(
                piece["out_start"], piece["out_end"], piece["lines"], piece.get("emphasis"), piece.get("style", "base")
            )
        )
    return "\n".join(lines_out) + "\n"


# ---------------------------------------------------------------------------
# vertical_hook: 縦書きテロップ（参考動画TTPの様式再現）
#
# ASSは真の縦組みレイアウトエンジンを持たないため、1文字ずつ `\N` で改行して縦1列に
# 積む簡易手法で疑似縦書きを実現する（Aegisub/AviUtl界隈で広く使われる手法）。
# `\pos(x,y)` で列の絶対位置を指定し、1列の最大文字数を超えたら日本語の縦書き慣習
# （右列が先・左へ追加）に合わせて左隣に次の列を追加する。
# フォントは実機確認の結果、"Hiragino Mincho ProN"は`/System/Library/Fonts/`直下には
# 存在せず(オンデマンドアセットのみでfontconfigからは解決できないことを実機確認済み)、
# 代わりにOFLライセンスの "Noto Serif JP Black"（既存のNoto Sans JP Blackと同系統・
# 太いウェイトでW6相当の重厚感）を採用した。assets/fonts/にフォントファイルを同梱し、
# ffmpegの`subtitles=...:fontsdir=`から解決させる（`fc-list`によるシステム全体の
# フォント検索には頼らない。既存のNoto Sans JP Blackと同じ方式）。
# ---------------------------------------------------------------------------

VERTICAL_HOOK_FONT = "Noto Serif JP Black"
VERTICAL_HOOK_FONT_SIZE = 78
VERTICAL_HOOK_OUTLINE = 6
VERTICAL_HOOK_SHADOW = 2
VERTICAL_HOOK_MAX_COL_CHARS = 10  # 1列の最大文字数（超えたら次の列へ）
VERTICAL_HOOK_TOP_Y = 90  # 画面上端からの開始y座標
VERTICAL_HOOK_RIGHT_X = 940  # 右端寄せアンカーのx座標（列の中心）
VERTICAL_HOOK_LEFT_X = 140  # 左端寄せアンカーのx座標（列の中心）
VERTICAL_HOOK_COL_STEP = 100  # 列が増えるごとのx方向オフセット

# 縦書きで倒れる/不自然になる文字の簡易置換（実機確認: 長音「ー」は1文字ずつ\N改行で
# 積むと横向きの棒のまま表示され不自然。fullwidth vertical bar「｜」に置換すると
# 縦棒として自然に見えることを実機レンダリングで確認した）。
_VERTICAL_HOOK_CHAR_SUBSTITUTIONS = {"ー": "｜"}


def _prepare_vertical_hook_text(text):
    out = []
    for ch in text or "":
        out.append(_VERTICAL_HOOK_CHAR_SUBSTITUTIONS.get(ch, ch))
    return out


def split_into_vertical_columns(text, max_col_chars=VERTICAL_HOOK_MAX_COL_CHARS):
    """縦書き用に文字列を列（各列は最大max_col_chars文字）に分割する。

    columns[0] が最初に読む列（画面上でも一番右に置く）、columns[1]以降は
    左に追加していく列になる（日本語の縦書き慣習: 右列が先）。
    """
    chars = _prepare_vertical_hook_text(text)
    if not chars:
        return []
    return [chars[i:i + max_col_chars] for i in range(0, len(chars), max_col_chars)]


def build_vertical_hook_ass_header():
    """vertical_hookスタイル用のASSヘッダ（Style: VerticalHook）を構築する。

    Alignmentは列ごとに`\\an8`（上中央）をDialogue側で明示するため、Style行の
    Alignment値そのものは実質参照されないが、フォーマット互換のため7(上左)を設定しておく。
    """
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: {play_res_x}\n"
        "PlayResY: {play_res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: VerticalHook,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,"
        "1,{outline},{shadow},7,20,20,20,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ).format(
        play_res_x=PLAY_RES_X, play_res_y=PLAY_RES_Y,
        font=VERTICAL_HOOK_FONT, size=VERTICAL_HOOK_FONT_SIZE,
        outline=VERTICAL_HOOK_OUTLINE, shadow=VERTICAL_HOOK_SHADOW,
    )


def build_vertical_hook_dialogue_lines(out_start, out_end, caption, anchor="right"):
    """1つのcaptionから、縦書き列ごとのDialogue行（1行以上）を組み立てる。

    anchor: "right"（右端寄せ・列は左へ追加）| "left"（左端寄せ・列は右へ追加）。
    ショットごとに交互に呼ぶことで、参考動画のように画面の左右でリズムを作る。
    """
    columns = split_into_vertical_columns(caption)
    if not columns:
        return []
    start = seconds_to_ass_time(out_start)
    end = seconds_to_ass_time(out_end)
    lines_out = []
    for col_idx, col_chars in enumerate(columns):
        if anchor == "left":
            x = VERTICAL_HOOK_LEFT_X + col_idx * VERTICAL_HOOK_COL_STEP
        else:
            x = VERTICAL_HOOK_RIGHT_X - col_idx * VERTICAL_HOOK_COL_STEP
        y = VERTICAL_HOOK_TOP_Y
        text = "\\N".join(escape_ass_text(c) for c in col_chars)
        override = "{{\\an8\\pos({},{})}}".format(x, y)
        lines_out.append(
            "Dialogue: 0,{},{},VerticalHook,,0,0,0,,{}{}".format(start, end, override, text)
        )
    return lines_out


def generate_vertical_hook_ass(telop_pieces):
    """縦書きテロップ(vertical_hookスタイル)のASS全文を生成する。

    ショットごとに右端/左端アンカーを交互に切り替える（出現順で偶数番目=右端、
    奇数番目=左端）。各pieceの"caption"（生キャプション文字列）を使う
    （"lines"は横書き禁則改行済みのため使わない）。
    """
    lines_out = [build_vertical_hook_ass_header().rstrip("\n")]
    sorted_pieces = sorted(telop_pieces, key=lambda p: p["out_start"])
    for i, piece in enumerate(sorted_pieces):
        caption = piece.get("caption")
        if caption is None:
            caption = "".join(piece.get("lines") or [])
        anchor = "right" if i % 2 == 0 else "left"
        lines_out.extend(
            build_vertical_hook_dialogue_lines(piece["out_start"], piece["out_end"], caption, anchor=anchor)
        )
    return "\n".join(lines_out) + "\n"


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

    助詞境界（句読点以外）は、直後の文字が「ひらがな以外」（漢字・カタカナ・
    英数字等）の場合にのみ有効とする。助詞の直後にひらがなが続く場合は
    「でできる」「にはじめる」のように動詞/補助動詞の一部（＝助詞そのものではない）
    である可能性が高く、採用すると語中分割（BUG-5: 「毎日5分でで／きる」等）になる
    ため無効とする。句読点（、。！？）の直後はこの制約を適用しない
    （句読点は常に有効な境界とみなす）。

    見つからなければNoneを返す（呼び出し側は文字数ベースの禁則改行にフォールバックする）。
    """
    limit = min(max_chars, len(text) - 1)
    for i in range(limit, 0, -1):
        if text[i] in FORBIDDEN_LINE_START:
            continue
        for ending in _PARTICLE_BREAK_ENDINGS:
            if not text[:i].endswith(ending):
                continue
            if ending not in _PUNCTUATION_BREAK_ENDINGS and _is_hiragana(text[i]):
                # 助詞直後がひらがな -> 動詞/補助動詞の一部の可能性が高く語中分割になるため無効
                continue
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
    if len(text) <= max_chars:
        # 全体がmax_chars以内に収まるなら、文中に助詞が含まれていても改行しない。
        return [text]
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
        # "caption"は生のキャプション文字列（vertical_hookスタイルの縦書き組版で使う。
        # "lines"は横書き禁則改行済みの行配列で、既定スタイルはこちらを使い続ける）。
        pieces.append({
            "out_start": out_start, "out_end": out_end, "lines": lines, "emphasis": [], "style": style,
            "caption": caption,
        })
    return pieces
