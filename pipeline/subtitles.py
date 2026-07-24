# -*- coding: utf-8 -*-
"""ASS字幕生成。PlayRes 1080x1920、Style Base/Big。

video-auto-editor/pipeline/subtitles.py のASS生成コア（ヘッダ/ダイアログ組み立て・
色タグ/特殊文字エスケープ）をそのまま移植し、reel向けに「shots配列（各ショット=
1キャプション、ショットの表示区間に同期）からtelop断片を組み立てる」ヘルパーを追加した。

DESIGN原則を踏襲: PlayRes=1080x1920, ASSはBGR順(`\\c&HBBGGRR&`)。
Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

try:
    import fcntl  # POSIX のみ（macOS/Linuxで有効）
except Exception:  # pragma: no cover - Windows等
    fcntl = None

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

STYLE_BASE_FONTSIZE = 76
STYLE_BIG_FONTSIZE = 92
MARGIN_L = 60
MARGIN_R = 60
MARGIN_V = 420
ALIGNMENT = 2  # bottom-center

# --- テロップの縁取り/影デザイン定数（「上品モダン+視認性最強」・目視後にここだけ調整） -------
# OutlineColour/BackColourはStyle行用の &HAABBGGRR 形式（AA=00は不透明、AAが大きいほど透明）。
OUTLINE = 2.5  # 縁の太さ（従来6は太すぎたため調整）
SHADOW = 3  # 影の距離
OUTLINE_COLOR = "&H001A1A1A"  # ダークチャコール（真っ黒より柔らかく上品な縁色）
BACK_COLOR = "&H80000000"  # 半透明黒（BorderStyle=1ではShadowの描画色として使われる=ソフトシャドウ）

_WHITE_RESET = "&HFFFFFF&"

_ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,Noto Sans JP Black,{base_size},&H00FFFFFF,&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1
Style: Big,Noto Sans JP Black,{big_size},&H00FFFFFF,&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

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


def _render_with_color_map(text, color_at, reset_color=None):
    parts = []
    i, n = 0, len(text)
    reset = reset_color or _WHITE_RESET
    while i < n:
        c = color_at[i]
        j = i
        while j < n and color_at[j] == c:
            j += 1
        chunk = escape_ass_text(text[i:j])
        if c is None:
            parts.append(chunk)
        else:
            # F9 fix: reset を hint 由来のベース色にできるようにする（None なら従来どおり白）。
            # 例: hint.color=yellow のとき accent span の後 \c は yellow に戻す
            #     （旧実装は常に白へ戻すため hint 由来のベース色を破壊していた）。
            parts.append("{{\\c{}}}{}{{\\c{}}}".format(c, chunk, reset))
        i = j
    return "".join(parts)


def build_dialogue_text(lines, emphasis=None, color_map=None, reset_color=None):
    """lines(行配列)からDialogue Textを組み立てる。

    color_map（joined文字列と同じ長さのリスト。各要素はBGRカラー文字列 or None）を
    明示指定した場合はそれを使う（自動アクセント抽出用）。未指定時は従来どおり
    emphasis（手動指定の強調区間）から _compute_color_map で算出する。
    reset_color（&HBBGGRR&）を渡すと、色付き span の直後に戻す `\\c` の値を上書きする
    （F9: hint.color がセットされているときは白ではなく hint 色に戻す）。
    """
    joined = "".join(lines)
    color_at = color_map if color_map is not None else _compute_color_map(joined, emphasis)
    rendered_lines = []
    cursor = 0
    for line in lines:
        segment_colors = color_at[cursor:cursor + len(line)]
        rendered_lines.append(_render_with_color_map(line, segment_colors, reset_color=reset_color))
        cursor += len(line)
    return "\\N".join(rendered_lines)


def build_ass_header(telop_style=None):
    """CLI(run.py)経路のASSヘッダを構築する。

    telop_style（None/str/dict）を渡すと、フォント/縁/座布団を反映した
    build_ass_header_with_style() の結果を返す（Studioの style-aware ヘッダと
    共通ロジックを使う）。未指定時は従来どおり固定テンプレート（完全後方互換）。
    """
    if telop_style is not None:
        return build_ass_header_with_style(subtitle_style=None, telop_style=telop_style)
    return _ASS_HEADER_TEMPLATE.format(
        play_res_x=PLAY_RES_X,
        play_res_y=PLAY_RES_Y,
        base_size=STYLE_BASE_FONTSIZE,
        big_size=STYLE_BIG_FONTSIZE,
        outline=OUTLINE,
        shadow=SHADOW,
        outline_color=OUTLINE_COLOR,
        back_color=BACK_COLOR,
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
Style: Base,{font},{base_size},&H00FFFFFF,&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,{spacing},0,{border_style},{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1
Style: Big,{font},{big_size},{big_primary},&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,{spacing},0,{border_style},{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# ---------------------------------------------------------------------------
# テロップ見た目バリエーション（telop_styles.json）
#
# 動画ごとに「フォント/縁/影/座布団」等の見た目を切り替えるための定義群。
# ttf内部ファミリー名で fontsdir 経由に libass が解決する（実機描画確認済み・
# 名前: Noto Sans JP Black / Noto Serif JP Black / Zen Maru Gothic Black /
# Mochiy Pop One / Yusei Magic / Klee One SemiBold / Rounded Mplus 1c Black）。
# ---------------------------------------------------------------------------

_TELOP_STYLES_PATH = Path(__file__).resolve().parent.parent / "assets" / "profiles" / "telop_styles.json"

# 既定スタイル（telop_styles.json未指定/読み込み失敗時のフォールバック=現行"shiro-futo"相当）。
# 既存テスト（Noto Sans JP Black / outline=2.5 / shadow=3 / OUTLINE_COLOR / BACK_COLOR / 座布団なし）
# の期待値と厳密に一致するよう組んである。変更する場合は test_subtitles_studio.py を要確認。
_DEFAULT_TELOP_STYLE_DEF = {
    "display_name": "白×太チャコール",
    "font": "Noto Sans JP Black",
    "base_size_scale": 1.0,
    "outline": OUTLINE,
    "shadow": SHADOW,
    "outline_color_hex": "#1A1A1A",
    "outline_color_alpha_hex": "00",
    "back_color_hex": "#000000",
    "back_color_alpha_hex": "80",
    "band": False,
    "spacing": 0,
    "margin_v_offset": 0,
    "accent_color_override": None,
    "vertical": False,
}

DEFAULT_TELOP_STYLE_NAME = "shiro-futo"
VERTICAL_TELOP_STYLE_NAME = "vertical-serif"

_TELOP_STYLES_CACHE = None


def _load_telop_styles_from_disk():
    try:
        with _TELOP_STYLES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    styles = data.get("styles")
    if not isinstance(styles, dict):
        return None
    horizontal_pool = data.get("horizontal_pool")
    if not isinstance(horizontal_pool, list) or not horizontal_pool:
        horizontal_pool = [DEFAULT_TELOP_STYLE_NAME]
    vertical_default = data.get("vertical_default") or VERTICAL_TELOP_STYLE_NAME
    return {"styles": styles, "horizontal_pool": horizontal_pool, "vertical_default": vertical_default}


def load_telop_styles():
    """assets/profiles/telop_styles.json を読み込んで返す（読込失敗時は最小フォールバック）。

    プロセス内でキャッシュする（テストで書き換えたい場合は _TELOP_STYLES_CACHE = None）。
    """
    global _TELOP_STYLES_CACHE
    if _TELOP_STYLES_CACHE is not None:
        return _TELOP_STYLES_CACHE
    data = _load_telop_styles_from_disk()
    if data is None:
        data = {
            "styles": {DEFAULT_TELOP_STYLE_NAME: dict(_DEFAULT_TELOP_STYLE_DEF)},
            "horizontal_pool": [DEFAULT_TELOP_STYLE_NAME],
            "vertical_default": VERTICAL_TELOP_STYLE_NAME,
        }
    _TELOP_STYLES_CACHE = data
    return data


def list_telop_style_names():
    """定義済みテロップスタイル名の一覧を返す（順序は telop_styles.json の"styles"順）。"""
    return list(load_telop_styles()["styles"].keys())


def resolve_telop_style_def(telop_style):
    """telop_style（None / スタイル名 str / 完全指定 dict）を _DEFAULT_TELOP_STYLE_DEF で補完して返す。

    - None: DEFAULT_TELOP_STYLE_NAME を解決して返す
    - str: telop_styles.json から名前解決。未知の名前は既定へフォールバック（例外は投げない）
    - dict: そのまま _DEFAULT_TELOP_STYLE_DEF とマージ（部分指定可）
    """
    if isinstance(telop_style, dict):
        merged = dict(_DEFAULT_TELOP_STYLE_DEF)
        for k, v in telop_style.items():
            if v is not None:
                merged[k] = v
        return merged
    data = load_telop_styles()
    name = telop_style if isinstance(telop_style, str) and telop_style else DEFAULT_TELOP_STYLE_NAME
    style_def = data["styles"].get(name)
    if not isinstance(style_def, dict):
        style_def = data["styles"].get(DEFAULT_TELOP_STYLE_NAME) or {}
    merged = dict(_DEFAULT_TELOP_STYLE_DEF)
    for k, v in style_def.items():
        if v is not None:
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# テロップスタイル履歴（直近回避）
#
# 直近1件（=前回動画で採用した horizontal_pool のstyle名）を候補プールから外して
# シード選択することで、seedが偶々近いmd5を出したときの「連続同スタイル」を防ぐ。
# 実装パターンは pipeline/bgm_library._save_history_to と同じ（flock排他 +
# tempfile→os.replaceのアトミック書き）。1責務: pick_telop_style_name の
# 内部ヘルパーとしてのみ使う。
# ---------------------------------------------------------------------------

_TELOP_HISTORY_PATH = Path(__file__).resolve().parent.parent / "assets" / "profiles" / ".telop_history.json"
_TELOP_HISTORY_MAX = 32
_TELOP_AVOID_LAST_N = 1  # 「直近1件のstyleを除外」（連続同スタイル防止）


def telop_history_path():
    return _TELOP_HISTORY_PATH


def _load_telop_history_from(path):
    if not path.exists():
        return {"recent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("recent"), list):
            return data
    except Exception:
        pass
    return {"recent": []}


def load_telop_history():
    return _load_telop_history_from(_TELOP_HISTORY_PATH)


def _save_telop_history_to(path, history):
    """history JSONをアトミック書き込み（tempfile→os.replace）＋flock排他で永続化する。

    pipeline/bgm_library._save_history_to と同じ実装パターン。
    - 同ディレクトリの .lock に fcntl.flock(EX) を取ってから書く（同時実行での破損回避）
    - tempfile を同一ディレクトリに作って os.replace で原子的に置換（途中失敗しても既存
      ファイルは常に妥当な JSON のまま残る）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(history, ensure_ascii=False, indent=2) + "\n"

    lock_path = path.parent / (path.name + ".lock")
    lock_fp = None
    if fcntl is not None:
        try:
            lock_fp = open(lock_path, "a+")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            if lock_fp is not None:
                try:
                    lock_fp.close()
                except Exception:
                    pass
                lock_fp = None

    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_name, str(path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            raise
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fp.close()
            except Exception:
                pass


def pick_telop_style_name(seed, preset=None, history=None, record_project_id=None,
                          history_path_override=None):
    """project_seed（str/int/None）から決定論的にテロップスタイル名を選ぶ。

    preset=="vertical_hook" のときは vertical_default（既定 "vertical-serif"）を返す
    （縦書きは連続同スタイル問題の対象外なので履歴回避は行わない）。
    それ以外（"default" or None）は horizontal_pool から seed で決定論選択する。
    履歴の直近1件のstyleは候補から除外する（連続同スタイル防止）。ただし候補が
    1個しか残らない場合は縮退して回避なしで返す。record_project_id を渡した場合、
    同 project_id が既に recent にあれば append 抑止（同一プロジェクトの再実行で
    決定論を維持するため）＝それ以外は新規に追記して永続化する。history_path_override
    を渡した場合はそのパスへ書き出す（テスト用）。
    """
    data = load_telop_styles()
    if preset == "vertical_hook":
        return data.get("vertical_default") or VERTICAL_TELOP_STYLE_NAME
    pool = data.get("horizontal_pool") or [DEFAULT_TELOP_STYLE_NAME]
    if not pool:
        return DEFAULT_TELOP_STYLE_NAME

    if history is None:
        history = load_telop_history()

    # 直近使用の回避: ただし自プロジェクトの過去エントリは「他人ではなく自分の履歴」
    # なので回避対象から外す（再実行で同スタイルを維持するため。決定論と回避の衝突回避）。
    recent_slice = history.get("recent", [])[-_TELOP_AVOID_LAST_N:]
    recent_names = [
        r.get("name") for r in recent_slice
        if isinstance(r, dict)
        and r.get("name")
        and not (record_project_id and r.get("project_id") == record_project_id)
    ]
    filtered = [n for n in pool if n not in recent_names]
    effective_pool = filtered if filtered else pool  # 候補1個なら回避なしで縮退

    key = "telop:{}".format(seed if seed is not None else "")
    digest = hashlib.md5(key.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big", signed=False) % len(effective_pool)
    chosen = effective_pool[idx]

    if record_project_id:
        recent_list = history.setdefault("recent", [])
        # 同じ project_id のエントリが既に recent にあれば append しない
        # （同プロジェクトの再実行で回避対象が積み重ならないようにする＝決定論維持）。
        already_recorded = any(
            isinstance(r, dict) and r.get("project_id") == record_project_id
            for r in recent_list
        )
        if not already_recorded:
            recent_list.append({
                "project_id": record_project_id,
                "name": chosen,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            history["recent"] = recent_list[-_TELOP_HISTORY_MAX:]
            _save_telop_history_to(
                Path(history_path_override) if history_path_override else _TELOP_HISTORY_PATH,
                history,
            )
    return chosen


def _telop_style_outline_color(style_def):
    """style_defからOutlineColour（Style行の&HAABBGGRR）を組み立てる。"""
    return _hex_to_ass_style_color(
        style_def.get("outline_color_hex") or "#1A1A1A",
        alpha_hex=style_def.get("outline_color_alpha_hex") or "00",
    )


def _telop_style_back_color(style_def):
    """style_defからBackColour（Style行の&HAABBGGRR）を組み立てる。"""
    return _hex_to_ass_style_color(
        style_def.get("back_color_hex") or "#000000",
        alpha_hex=style_def.get("back_color_alpha_hex") or "80",
    )


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


def build_ass_header_with_style(subtitle_style=None, telop_style=None):
    """subtitle_style（font_size/accent_color/position）+ telop_style（見た目）を反映したASSヘッダを構築する。

    Studio専用のテロップ再生成で使う（既存の build_ass_header()/generate_ass() は
    run.py のCLI経路向けとして変更せず残す）。DEFAULT_SUBTITLE_STYLE を既定値とし、
    Big(フック)スタイルの文字色に accent_color を反映する。

    telop_style（None/str/dict）は resolve_telop_style_def() 経由で解決し、
    フォント名/サイズスケール/縁色/座布団(BorderStyle=3)/字間 を Style 行へ焼き込む。
    未指定時は既定"shiro-futo"相当 → Noto Sans JP Black + 現行の縁2.5/影3/ダーク
    チャコール縁で完全後方互換（既存テストの数値がそのまま維持される）。
    """
    style = resolve_subtitle_style(subtitle_style)
    telop_def = resolve_telop_style_def(telop_style)

    scale = float(telop_def.get("base_size_scale") or 1.0)
    base_size = int(round((style.get("font_size") or STYLE_BASE_FONTSIZE) * scale))
    big_size = base_size + (STYLE_BIG_FONTSIZE - STYLE_BASE_FONTSIZE)
    margin_v = _POSITION_MARGIN_V.get(style.get("position"), MARGIN_V) + int(telop_def.get("margin_v_offset") or 0)
    # Big(フック)行の全文をaccent_colorで塗る旧仕様は、明るい背景で細縁と
    # 組み合わさると可読性が崩れることが実機フレーム目視で判明したため廃止。
    # フック行も白ベースとし、強調は自動アクセント(部分着色)に一本化する。
    big_primary = "&H00FFFFFF"

    # band=Trueは BorderStyle=3（不透明ボックス=座布団）でOutlineColour色の帯を
    # テキスト背景に描く（padding量はOutlineフィールドが担う）。band=Falseは従来
    # BorderStyle=1（縁取り+影）。libassの実挙動を実機フレームで確認済み。
    if telop_def.get("band"):
        border_style = 3
    else:
        border_style = 1

    return _STYLED_ASS_HEADER_TEMPLATE.format(
        play_res_x=PLAY_RES_X,
        play_res_y=PLAY_RES_Y,
        font=telop_def.get("font") or "Noto Sans JP Black",
        base_size=base_size,
        big_size=big_size,
        big_primary=big_primary,
        outline=telop_def.get("outline", OUTLINE),
        shadow=telop_def.get("shadow", SHADOW),
        outline_color=_telop_style_outline_color(telop_def),
        back_color=_telop_style_back_color(telop_def),
        alignment=ALIGNMENT,
        margin_l=MARGIN_L,
        margin_r=MARGIN_R,
        margin_v=margin_v,
        spacing=int(telop_def.get("spacing") or 0),
        border_style=border_style,
    )


# ---------------------------------------------------------------------------
# 自動アクセント抽出: 数字+単位／「」内／商品名トークンを自動で強調着色する。
# 既存の _compute_color_map（emphasis由来・text.findで全出現に着色）とは別モノとして
# 新設する（自動抽出はcaption内の限定span=最大limit件だけに着色したいため、全出現
# 着色の副作用がある既存関数はそのまま使わない）。
# ---------------------------------------------------------------------------

ACCENT_SPAN_LIMIT = 2
_ACCENT_COVERAGE_RATIO_LIMIT = 0.6  # 着色合計文字数がcaption全長に対してこの比率を超えたら超過span破棄
_ACCENT_MIN_CAPTION_LEN = 4  # これ未満のcaptionはアクセント抽出しない

_ACCENT_PRIORITY_PRODUCT = 3
_ACCENT_PRIORITY_NUMBER_UNIT = 2
_ACCENT_PRIORITY_BRACKET = 1

# 数字+単位: 長い単位を先にマッチさせるため長さ降順に並べる（例: "kg"を"g"より先に）。
_ACCENT_UNITS = tuple(
    sorted(
        [
            "日目", "日間", "週間", "時間", "ヶ月", "か月", "カ月", "年間",
            "kg", "mL", "ml", "cc",
            "円", "年", "分", "秒", "割", "倍", "歳", "回", "個", "本", "枚", "g", "%", "％",
        ],
        key=len,
        reverse=True,
    )
)
_ACCENT_NUMBER_UNIT_RE = re.compile(
    r"[0-9０-９]+(?:[.．][0-9０-９]+)?(?:" + "|".join(re.escape(u) for u in _ACCENT_UNITS) + ")"
)
_ACCENT_BRACKET_RE = re.compile(r"「([^「」]{1,12})」")
_ACCENT_PRODUCT_TOKEN_SPLIT_RE = re.compile(r"[\s\-–・／]+")  # 空白/-/–/・/／

# 商品名トークンの最小文字数（2文字だと一般語がヒットしやすいため3文字に引き上げ）。
_ACCENT_PRODUCT_TOKEN_MIN_LEN = 3

# 商品カテゴリの頻出一般名詞（商品名トークンとして単独採用すると、caption中の無関係な
# 同じ単語まで着色してしまう「一般語誤爆」の原因になるため除外する）。
_ACCENT_PRODUCT_TOKEN_STOPWORDS = frozenset([
    "美容", "化粧", "セット", "クリーム", "オイル", "シャンプー",
    "トリートメント", "マスク", "パック", "ジェル", "ローション",
    "スプレー", "ブラシ", "タオル", "ケース",
])

# 西暦表記（4桁数字+「年」）を数字+単位アクセントの対象外にするための判定用。
_ACCENT_YEAR_UNIT = "年"
_ACCENT_YEAR_DIGIT_LEN = 4


def _product_name_candidates(product_name):
    """product_nameを空白/-/–/・/／で分割したトークン(3文字以上・一般語ストップリスト除外)
    + フル文字列を、長い順に返す。"""
    if not product_name:
        return []
    tokens = [
        t for t in _ACCENT_PRODUCT_TOKEN_SPLIT_RE.split(product_name)
        if len(t) >= _ACCENT_PRODUCT_TOKEN_MIN_LEN and t not in _ACCENT_PRODUCT_TOKEN_STOPWORDS
    ]
    candidates = list(dict.fromkeys(tokens + [product_name]))  # 重複除去（順序維持）
    candidates.sort(key=len, reverse=True)
    return candidates


def _is_calendar_year_match(matched_text):
    """数字+単位マッチが「4桁数字+年」（例: 2026年）＝西暦表記かどうかを判定する。

    西暦は強調すべきアクセントではないため対象外にする。「5年」等の短い年数表記は
    4桁ではないため対象外にならず、引き続きアクセント対象になる。
    """
    if not matched_text.endswith(_ACCENT_YEAR_UNIT):
        return False
    digits = matched_text[:-len(_ACCENT_YEAR_UNIT)]
    if "." in digits or "．" in digits:
        return False
    return len(digits) == _ACCENT_YEAR_DIGIT_LEN


def _collect_raw_accent_spans(caption, product_name):
    """caption内の (start, end, priority) 候補spanを優先度別にすべて集める（重複解消前）。"""
    spans = []
    for token in _product_name_candidates(product_name):
        idx = caption.find(token)
        if idx >= 0:
            spans.append((idx, idx + len(token), _ACCENT_PRIORITY_PRODUCT))
    for m in _ACCENT_NUMBER_UNIT_RE.finditer(caption):
        if _is_calendar_year_match(m.group()):
            continue
        spans.append((m.start(), m.end(), _ACCENT_PRIORITY_NUMBER_UNIT))
    for m in _ACCENT_BRACKET_RE.finditer(caption):
        spans.append((m.start(1), m.end(1), _ACCENT_PRIORITY_BRACKET))  # 括弧の中身のみ
    return spans


def extract_accent_spans(caption, product_name=None, limit=ACCENT_SPAN_LIMIT):
    """captionから自動で強調着色すべきspan列 [(start, end), ...] を抽出する（副作用なし）。

    優先度: 商品名 > 数字+単位 > 「」。重複spanは高優先を採用する。limitで選抜する際も
    優先度降順(同優先度内は開始位置昇順)を先に適用してから先頭limit件を採用する
    （開始位置順で先に切ると、後方にある高優先spanがlimit選抜から漏れてしまうため）。
    最終的な返り値は開始位置順に並べ替える。着色合計文字数がcaption全長の60%を超える
    場合は超過分を破棄する。caption全長が4文字未満なら抽出しない。
    """
    if not caption or len(caption) < _ACCENT_MIN_CAPTION_LEN:
        return []
    raw_spans = _collect_raw_accent_spans(caption, product_name)
    if not raw_spans:
        return []

    # 優先度降順・開始位置昇順で走査し、既採用spanと重なるものは捨てる(=高優先が残る)。
    raw_spans.sort(key=lambda s: (-s[2], s[0]))
    accepted = []
    for start, end, priority in raw_spans:
        overlaps = any(start < a_end and end > a_start for a_start, a_end, _ in accepted)
        if overlaps:
            continue
        accepted.append((start, end, priority))

    # 優先度降順(=既にこの順)のままlimit選抜してから、返却用に開始位置順へ並べ替える。
    accepted = accepted[:limit]
    accepted.sort(key=lambda s: s[0])

    total_len = len(caption)
    result = []
    colored = 0
    for start, end, _priority in accepted:
        span_len = end - start
        if colored + span_len > total_len * _ACCENT_COVERAGE_RATIO_LIMIT:
            continue
        result.append((start, end))
        colored += span_len
    return result


def _color_map_from_spans(length, spans, accent_bgr):
    """(start, end)のspan列から、_render_with_color_map が使う長さlengthの色マップを組み立てる。"""
    color_at = [None] * length
    for start, end in spans:
        for i in range(max(0, start), min(length, end)):
            color_at[i] = accent_bgr
    return color_at


def generate_ass_with_style(telop_pieces, subtitle_style=None, product_name=None, animation_enabled=True,
                            telop_style=None):
    """generate_ass() のsubtitle_style対応版。Studioのテロップ再生成で使う。

    subtitle_style.preset=="vertical_hook" のときは横書き(Base/Big)ではなく、
    generate_vertical_hook_ass() による縦書きテロップを生成する。

    product_name（商品モードのproduct.name）を渡すと、自動アクセント抽出
    （数字+単位／「」内／商品名トークン）でstyle!="big"かつemphasis未指定のpieceに
    自動着色する（Big行はStudioヘッダのBig PrimaryColour=accent_colorのため、
    \\cで白リセットすると壊れるので絶対に着色しない）。

    telop_style（None/str/dict）でテロップの見た目バリエーション（フォント/縁/座布団等）
    を切り替える。未指定は既定"shiro-futo"（Noto Sans JP Black・現行仕様と一致）。
    style.accent_color は telop_def.accent_color_override があればそちらを優先する。
    """
    style = resolve_subtitle_style(subtitle_style)
    telop_def = resolve_telop_style_def(telop_style)
    if style.get("preset") == "vertical_hook":
        return generate_vertical_hook_ass(
            telop_pieces, subtitle_style=subtitle_style, product_name=product_name,
            animation_enabled=animation_enabled, telop_style=telop_style,
        )

    accent_color_hex = (
        telop_def.get("accent_color_override")
        or style.get("accent_color")
        or DEFAULT_SUBTITLE_STYLE["accent_color"]
    )
    accent_bgr = hex_to_ass_bgr(accent_color_hex)

    lines_out = [build_ass_header_with_style(subtitle_style, telop_style=telop_style).rstrip("\n")]
    for piece in sorted(telop_pieces, key=lambda p: p["out_start"]):
        piece_style = piece.get("style", "base")
        lines = piece["lines"]
        emphasis = piece.get("emphasis")
        # Big行のPrimaryが白になったため(明るい背景での可読性対策)、
        # フック行にも自動アクセントを適用できる(白リセットで壊れない)。
        color_map = None
        if not emphasis:
            joined = "".join(lines)
            spans = extract_accent_spans(joined, product_name=product_name)
            if spans:
                color_map = _color_map_from_spans(len(joined), spans, accent_bgr)
        lines_out.append(
            build_dialogue_line(
                piece["out_start"], piece["out_end"], lines, emphasis, piece_style, color_map=color_map,
                animation_enabled=animation_enabled,
                font_px_override=piece.get("font_px_override"),
                telop_style_hint=piece.get("telop_style_hint"),
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
VERTICAL_HOOK_OUTLINE = 3
VERTICAL_HOOK_SHADOW = 3
VERTICAL_HOOK_OUTLINE_COLOR = OUTLINE_COLOR  # ダークチャコール（横書きヘッダと統一）
VERTICAL_HOOK_BACK_COLOR = BACK_COLOR  # 半透明黒（ソフトシャドウ色）
VERTICAL_HOOK_MAX_COL_CHARS = 10  # 1列の最大文字数（超えたら次の列へ）
VERTICAL_HOOK_TOP_Y = 90  # 画面上端からの開始y座標
VERTICAL_HOOK_RIGHT_X = 940  # 右端寄せアンカーのx座標（列の中心）
VERTICAL_HOOK_LEFT_X = 140  # 左端寄せアンカーのx座標（列の中心）
VERTICAL_HOOK_COL_STEP = 100  # 列が増えるごとのx方向オフセット
VERTICAL_HOOK_COL_STAGGER_SEC = 0.09  # 列(2列目以降)ごとのStart遅延
VERTICAL_HOOK_COL_STAGGER_MIN_REMAIN_SEC = 0.3  # 遅延後もout_endからこの秒数は残す（クランプ）

VERTICAL_FAD_IN_MS = 140
VERTICAL_FAD_OUT_MS = 100
VERTICAL_FAD_SHORT_IN_MS = 70
VERTICAL_FAD_SHORT_OUT_MS = 50


def _vertical_fad_for(duration_sec):
    """縦書き列の表示尺(duration_sec)に応じた`\\fad(...)`タグ本体（括弧含む）を返す。"""
    if duration_sec is not None and duration_sec < FAD_SHORT_THRESHOLD_SEC:
        return "fad({},{})".format(VERTICAL_FAD_SHORT_IN_MS, VERTICAL_FAD_SHORT_OUT_MS)
    return "fad({},{})".format(VERTICAL_FAD_IN_MS, VERTICAL_FAD_OUT_MS)

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


def build_vertical_hook_ass_header(telop_style=None):
    """vertical_hookスタイル用のASSヘッダ（Style: VerticalHook）を構築する。

    Alignmentは列ごとに`\\an8`（上中央）をDialogue側で明示するため、Style行の
    Alignment値そのものは実質参照されないが、フォーマット互換のため7(上左)を設定しておく。
    telop_style（None/str/dict）を渡すと、フォント名/縁色/縁の太さを上書きできる。
    未指定時は従来どおり VERTICAL_HOOK_* 定数で構築（完全後方互換）。
    """
    if telop_style is None:
        font = VERTICAL_HOOK_FONT
        size = VERTICAL_HOOK_FONT_SIZE
        outline = VERTICAL_HOOK_OUTLINE
        shadow = VERTICAL_HOOK_SHADOW
        outline_color = VERTICAL_HOOK_OUTLINE_COLOR
        back_color = VERTICAL_HOOK_BACK_COLOR
    else:
        telop_def = resolve_telop_style_def(telop_style)
        font = telop_def.get("font") or VERTICAL_HOOK_FONT
        size = int(round(VERTICAL_HOOK_FONT_SIZE * float(telop_def.get("base_size_scale") or 1.0)))
        outline = telop_def.get("outline", VERTICAL_HOOK_OUTLINE)
        shadow = telop_def.get("shadow", VERTICAL_HOOK_SHADOW)
        outline_color = _telop_style_outline_color(telop_def)
        back_color = _telop_style_back_color(telop_def)
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
        "Style: VerticalHook,{font},{size},&H00FFFFFF,&H000000FF,{outline_color},{back_color},0,0,0,0,100,100,0,0,"
        "1,{outline},{shadow},7,20,20,20,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ).format(
        play_res_x=PLAY_RES_X, play_res_y=PLAY_RES_Y,
        font=font, size=size,
        outline=outline, shadow=shadow,
        outline_color=outline_color, back_color=back_color,
    )


def _vertical_col_start_sec(out_start, out_end, col_idx):
    """列col_idx(0始まり)のDialogue Startを組み立てる（2列目以降は+0.09秒/列ずつ遅らせる）。

    out_end - VERTICAL_HOOK_COL_STAGGER_MIN_REMAIN_SEC を超えないようクランプする
    （表示尺が極端に短いcaptionで列の表示時間がゼロ/負にならないようにする保険）。
    """
    delayed = out_start + col_idx * VERTICAL_HOOK_COL_STAGGER_SEC
    clamp_ceiling = out_end - VERTICAL_HOOK_COL_STAGGER_MIN_REMAIN_SEC
    delayed = min(delayed, clamp_ceiling)
    return max(delayed, out_start)


def build_vertical_hook_dialogue_lines(out_start, out_end, caption, anchor="right", color_map=None,
                                       animation_enabled=True):
    """1つのcaptionから、縦書き列ごとのDialogue行（1行以上）を組み立てる。

    anchor: "right"（右端寄せ・列は左へ追加）| "left"（左端寄せ・列は右へ追加）。
    ショットごとに交互に呼ぶことで、参考動画のように画面の左右でリズムを作る。
    color_map: caption（置換前の原文）と同じ長さの色マップ（各要素はBGRカラー文字列 or None）。
    列=別Dialogueイベントのため\\cは列をまたいで持続しない。列ごとに必ず色タグを
    張り直す必要があるため、列内の各文字ごとに個別の{\\c...}...{\\c...}を挿入する。
    """
    columns = split_into_vertical_columns(caption)
    if not columns:
        return []
    end = seconds_to_ass_time(out_end)
    lines_out = []
    for col_idx, col_chars in enumerate(columns):
        if anchor == "left":
            x = VERTICAL_HOOK_LEFT_X + col_idx * VERTICAL_HOOK_COL_STEP
        else:
            x = VERTICAL_HOOK_RIGHT_X - col_idx * VERTICAL_HOOK_COL_STEP
        y = VERTICAL_HOOK_TOP_Y
        # animation_enabled=False のときは列stagger（2列目以降のStart遅延）を無効化し、
        # 全列を out_start で同時にカットイン表示する。
        if animation_enabled:
            col_start_sec = _vertical_col_start_sec(out_start, out_end, col_idx)
        else:
            col_start_sec = out_start
        start = seconds_to_ass_time(col_start_sec)
        col_duration = out_end - col_start_sec
        fad = _vertical_fad_for(col_duration)

        col_char_offset = sum(len(c) for c in columns[:col_idx])
        rendered_chars = []
        for offset, ch in enumerate(col_chars):
            char_idx = col_char_offset + offset
            color = color_map[char_idx] if color_map and char_idx < len(color_map) else None
            esc = escape_ass_text(ch)
            if color:
                rendered_chars.append("{{\\c{}}}{}{{\\c{}}}".format(color, esc, _WHITE_RESET))
            else:
                rendered_chars.append(esc)
        text = "\\N".join(rendered_chars)
        # animation_enabled=False のときは \fad を付けず即時カットインにする。
        if animation_enabled:
            override = "{{\\an8\\pos({},{})\\{}}}".format(x, y, fad)
        else:
            override = "{{\\an8\\pos({},{})}}".format(x, y)
        lines_out.append(
            "Dialogue: 0,{},{},VerticalHook,,0,0,0,,{}{}".format(start, end, override, text)
        )
    return lines_out


def generate_vertical_hook_ass(telop_pieces, subtitle_style=None, product_name=None, animation_enabled=True,
                               telop_style=None):
    """縦書きテロップ(vertical_hookスタイル)のASS全文を生成する。

    ショットごとに右端/左端アンカーを交互に切り替える（出現順で偶数番目=右端、
    奇数番目=左端）。各pieceの"caption"（生キャプション文字列）を使う
    （"lines"は横書き禁則改行済みのため使わない）。

    product_nameを渡すと自動アクセント抽出（数字+単位／「」内／商品名トークン）を
    置換前のcaption原文に対して行う（「ー→｜」置換は1文字1対1のためindexはそのまま
    使える）。accent_colorはsubtitle_styleから解決する（既定#FFD84D。telop_styleに
    accent_color_overrideがあればそちらを優先）。telop_style未指定時は従来のVERTICAL_HOOK_*
    定数どおり（後方互換）。
    """
    style = resolve_subtitle_style(subtitle_style)
    telop_def = resolve_telop_style_def(telop_style) if telop_style is not None else None
    accent_color_hex = (
        (telop_def.get("accent_color_override") if telop_def else None)
        or style.get("accent_color")
        or DEFAULT_SUBTITLE_STYLE["accent_color"]
    )
    accent_bgr = hex_to_ass_bgr(accent_color_hex)

    lines_out = [build_vertical_hook_ass_header(telop_style=telop_style).rstrip("\n")]
    sorted_pieces = sorted(telop_pieces, key=lambda p: p["out_start"])
    for i, piece in enumerate(sorted_pieces):
        caption = piece.get("caption")
        if caption is None:
            caption = "".join(piece.get("lines") or [])
        anchor = "right" if i % 2 == 0 else "left"
        color_map = None
        spans = extract_accent_spans(caption, product_name=product_name)
        if spans:
            color_map = _color_map_from_spans(len(caption), spans, accent_bgr)
        lines_out.extend(
            build_vertical_hook_dialogue_lines(
                piece["out_start"], piece["out_end"], caption, anchor=anchor, color_map=color_map,
                animation_enabled=animation_enabled,
            )
        )
    return "\n".join(lines_out) + "\n"


# --- テロップのフェード+ポップイン（\fad + \fscx/\fscy + \t） -----------------------
# 「上品モダン+視認性最強」の一環として、全Dialogueにフェードイン/アウトと軽いポップ
# （94%->100%へ120msで拡大）を付与する。表示尺が短いテロップは既定のフェード長だと
# フェードだけで表示時間の大半を使ってしまうため短縮する。
FAD_IN_MS = 160
FAD_OUT_MS = 120
FAD_SHORT_IN_MS = 80
FAD_SHORT_OUT_MS = 60
FAD_SHORT_THRESHOLD_SEC = 0.6
POP_SCALE_START_PCT = 94
POP_DURATION_MS = 120


def _fad_for(duration_sec):
    """表示尺(duration_sec)に応じたASSの`\\fad(...)`タグ本体（括弧含む）を返す。"""
    if duration_sec is not None and duration_sec < FAD_SHORT_THRESHOLD_SEC:
        return "fad({},{})".format(FAD_SHORT_IN_MS, FAD_SHORT_OUT_MS)
    return "fad({},{})".format(FAD_IN_MS, FAD_OUT_MS)


def _pop_override_block(duration_sec):
    """行頭に付与する `{\\fad(...)\\fscx94\\fscy94\\t(0,120,\\fscx100\\fscy100)}` を組み立てる。

    初期値の\\fscx94\\fscy94は\\tより前に置く（後置だと\\tの目標値で上書きされてしまう）。
    """
    return "{{\\{}\\fscx{p}\\fscy{p}\\t(0,{d},\\fscx100\\fscy100)}}".format(
        _fad_for(duration_sec), p=POP_SCALE_START_PCT, d=POP_DURATION_MS
    )


def build_dialogue_line(out_start, out_end, lines, emphasis=None, style="base", color_map=None,
                        animation_enabled=True, font_px_override=None, telop_style_hint=None):
    style_name = "Big" if style == "big" else "Base"
    start = seconds_to_ass_time(out_start)
    end = seconds_to_ass_time(out_end)
    duration_sec = None
    if out_start is not None and out_end is not None:
        duration_sec = out_end - out_start
    # animation_enabled=False のときは \fad/\fscx/\fscy/\t 系のアニメ指定を一切付けない
    # （テロップをカットインで即時表示する。既定Trueは完全後方互換）。
    override = _pop_override_block(duration_sec) if animation_enabled else ""
    # BUG-54: piece に font_px_override があれば Dialogue にインラインで {\fs<n>} を付与し
    # そのcaptionだけフォントを縮小する(Style行は既定サイズ据え置き)。
    if font_px_override is not None:
        override = "{{\\fs{}}}".format(int(font_px_override)) + override
    # F9: telop_style_hint（参考動画の position/color）を Dialogue の inline override へ焼き込む。
    # 既存の pop override より前に置いて、\an/\c で位置と色を上書きする。
    hint_prefix = build_hint_override_prefix(telop_style_hint) if telop_style_hint else None
    if hint_prefix:
        override = "{{{}}}".format(hint_prefix) + override
    # F9 fix (codex-review P1): hint.color が指定されているときは、accent span の
    # 直後の \c 戻し先を hint 色にする（デフォルト白リセットでは hint 色が破壊される）。
    reset_color = _resolve_hint_color_bgr(telop_style_hint) if telop_style_hint else None
    text = override + build_dialogue_text(lines, emphasis, color_map=color_map, reset_color=reset_color)
    return "Dialogue: 0,{},{},{},,0,0,0,,{}".format(start, end, style_name, text)


def generate_ass(telop_pieces, animation_enabled=True, telop_style=None):
    """出力時刻マップ済みのtelop断片群からASS全文を生成する。

    telop_pieces: [{"out_start","out_end","lines","emphasis","style"}, ...]
    animation_enabled=False で編集プロファイル(telop.animation=="none")のカットイン表示に
    合わせて \\fad/\\fscx/\\fscy/\\t 系のタグを一切付けない（既定Trueは完全後方互換）。
    telop_style（None/str/dict）を渡すと、フォント/縁/座布団のバリエーションを反映する。
    """
    lines_out = [build_ass_header(telop_style=telop_style).rstrip("\n")]
    for piece in sorted(telop_pieces, key=lambda p: p["out_start"]):
        lines_out.append(
            build_dialogue_line(
                piece["out_start"], piece["out_end"], piece["lines"], piece.get("emphasis"), piece.get("style", "base"),
                animation_enabled=animation_enabled,
                font_px_override=piece.get("font_px_override"),
                telop_style_hint=piece.get("telop_style_hint"),
            )
        )
    return "\n".join(lines_out) + "\n"


# ---------------------------------------------------------------------------
# reel固有: shots配列 → telop断片（各ショットの表示区間＝ナレーションの対応区間に同期）
# ---------------------------------------------------------------------------

def _iter_break_candidates(text, max_chars):
    """text内でmax_chars以内に収まる、有効な文節/句読点境界の候補位置をすべて昇順で列挙する。

    有効判定は次の規則（BUG-2/BUG-5対応の簡易文節分割ルール）に従う:
    - 行頭禁則文字（FORBIDDEN_LINE_START）の直前では区切らない。
    - 助詞境界（句読点以外）は、直後の文字が「ひらがな以外」（漢字・カタカナ・英数字等）
      の場合にのみ有効。助詞の直後にひらがなが続く場合は「でできる」「にはじめる」の
      ように動詞/補助動詞の一部（＝助詞そのものではない）である可能性が高く、採用すると
      語中分割（BUG-5）になるため無効。
    - 句読点（、。！？）の直後はこの制約を適用しない（常に有効な境界とみなす）。

    返り値: 有効な境界位置(int)のリスト（昇順）。見つからなければ空リスト。
    """
    limit = min(max_chars, len(text) - 1)
    candidates = []
    for i in range(1, limit + 1):
        if text[i] in FORBIDDEN_LINE_START:
            continue
        for ending in _PARTICLE_BREAK_ENDINGS:
            if not text[:i].endswith(ending):
                continue
            if ending not in _PUNCTUATION_BREAK_ENDINGS and _is_hiragana(text[i]):
                # 助詞直後がひらがな -> 動詞/補助動詞の一部の可能性が高く語中分割になるため無効
                continue
            candidates.append(i)
            break
    return candidates


def _find_bunsetsu_break(text, max_chars):
    """text内でmax_chars以内に収まる最も右側の文節/句読点境界を探す（簡易文節分割）。

    _iter_break_candidates の候補列挙をそのまま使い、最右端（＝従来どおり右側優先）の
    候補を返すラッパ。既存の直接呼び出しテスト（右側優先の境界を期待するもの）との
    互換性を保つために残してある。見つからなければNoneを返す（呼び出し側は文字数
    ベースの禁則改行にフォールバックする）。
    """
    candidates = _iter_break_candidates(text, max_chars)
    return candidates[-1] if candidates else None


def _pick_balanced_break(text, max_chars):
    """2行に分けたときの行長バランスが最良の改行位置を選ぶ（「上品モダン」の行長揃え）。

    _iter_break_candidates の候補のうち「2行目がmax_chars以内に収まる」ものだけを対象に、
    |1行目の長さ - 2行目の長さ| が最小の候補を選ぶ（同点の場合はより右側＝1行目が長い方を
    優先する）。対象候補が無ければNoneを返す（呼び出し側は文字数ベースの禁則改行に
    フォールバックする）。
    """
    best = None
    best_diff = None
    for i in _iter_break_candidates(text, max_chars):
        line2_len = len(text) - i
        if line2_len > max_chars:
            continue
        diff = abs(i - line2_len)
        if best is None or diff <= best_diff:
            best = i
            best_diff = diff
    return best


# --- 幅ベースの折り返し（BUG-54: 見切れ対策） -------------------------------------
# 「1行に収まる字数」を字数固定ではなく描画幅で算出し、収まらなければフォントサイズを
# 段階縮小して再計算する。全角=フォントpx、半角=フォントpx*0.5 で近似する
# （日本語主体captionでは十分な精度）。
CAPTION_SAFE_WIDTH_RATIO = 0.90  # PlayResX=1080 × 0.9 = 972 を上限に MarginL/R を引く
CAPTION_FONT_STEP_PX = 8  # 縮小ステップ幅
CAPTION_MIN_FONT_PX = 48  # フォント縮小の下限（下線割れ・可読性下限）


def _char_visual_width_px(ch, font_px):
    """1文字の描画幅の近似(px)。半角(ASCII)は0.5倍、全角は1倍の font_px 相当。"""
    if ord(ch) < 128:
        return float(font_px) * 0.5
    return float(font_px) * 1.0


def _line_visual_width_px(line, font_px):
    return sum(_char_visual_width_px(c, font_px) for c in line)


def caption_safe_width_px(margin_l=MARGIN_L, margin_r=MARGIN_R):
    """1行に使える横幅(px)。PlayResX * ratio - MarginL/R を返す。"""
    return float(PLAY_RES_X) * CAPTION_SAFE_WIDTH_RATIO - float(margin_l) - float(margin_r)


def wrap_caption_by_width(text, font_px, safe_width_px=None, max_lines=2,
                          min_font_px=CAPTION_MIN_FONT_PX, font_step_px=CAPTION_FONT_STEP_PX):
    """描画幅ベースで折り返し、収まらなければフォントを段階縮小して再試行する。

    Returns: (lines: List[str], effective_font_px: int)
    - font_px を初期値として、1行あたりの最大文字数を floor(safe_width_px / font_px)
      として wrap_caption_kinsoku で折り返す(既存の文節境界・禁則ロジックを継続利用)。
    - 分割後の各行の実測描画幅が safe_width_px 以下、かつ行数が max_lines 以内なら OK。
    - どちらかを満たさなければ font_px -= font_step_px して再計算。min_font_px を下回っても
      収まらない場合は「min_font_px でベストエフォート」の結果を返す(3行以上/若干見切れの
      可能性は残るがレンダを止めないため)。
    """
    text = (text or "").strip()
    if not text:
        return [], int(font_px)
    if safe_width_px is None:
        safe_width_px = caption_safe_width_px()
    safe_width_px = float(safe_width_px)
    tried = int(font_px)
    while True:
        max_chars = max(1, int(safe_width_px // tried))  # 全角1文字=font_pxとみなす保守寄り
        lines = wrap_caption_kinsoku(text, max_chars=max_chars, max_lines=max_lines)
        fits_rows = len(lines) <= max_lines
        fits_width = all(_line_visual_width_px(ln, tried) <= safe_width_px + 1e-6 for ln in lines)
        if fits_rows and fits_width:
            return lines, tried
        next_try = tried - int(font_step_px)
        if next_try < int(min_font_px):
            # 下限に達しても収まらない場合: 下限フォントで最終計算(3行以上になるかもしれない)。
            fallback_chars = max(1, int(safe_width_px // int(min_font_px)))
            fallback_lines = wrap_caption_kinsoku(text, max_chars=fallback_chars, max_lines=max_lines)
            return fallback_lines, int(min_font_px)
        tried = next_try


def wrap_caption_kinsoku(text, max_chars=13, max_lines=2):
    """caption_jpを最大max_lines行に分割する。

    まず文節/句読点の境界（簡易分割）候補の中から、2行の長さバランスが最良のもので
    折る（「リセ／ット」のような語中分割を避けつつ、行の見た目の長さを揃える）。
    候補が無い場合のみ、従来どおり13字禁則改行（行頭禁則文字は前行へ繰り込み）に
    フォールバックする。
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
        cut = _pick_balanced_break(remaining, max_chars)
        if cut is None:
            cut = min(max_chars, len(remaining))
            while cut < len(remaining) and remaining[cut] in FORBIDDEN_LINE_START:
                cut += 1
        lines.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        lines.append(remaining)
    return lines


# ---------------------------------------------------------------------------
# F9: telop_style_hint を ASS の描画属性へ写像する
#
# skeleton から plan.shots に載っている telop_style_hint（参考動画の
# position/color/size_class/stroke/emphasis_words）を、Dialogue 行の inline
# オーバライド（\pos/\c/\fs/\bord/\fscx-\fscy）に反映するためのヘルパー群。
#
# 監査 F9 の要点: 現行実装は telop_style_hint を validate では保持するが
# subtitles では一切参照されていなかった。ここで
# build_telop_pieces_from_shots に伝播→ generate_ass_with_style / generate_ass
# で Dialogue 行に focused override として焼き込む。
# ---------------------------------------------------------------------------

# 参考spec の position 語 → ASS Alignment/MarginV の写像。
# Alignment: 8=top-center, 5=middle-center(注: \an5 は 5番, ASSは 7=upperleft等),
# 実装は \an8/\an5/\an2 を使う（\pos と併用時は Alignment 上書きになる）。
_HINT_POSITION_ALIGNMENT = {
    "top": 8,     # 上寄せ
    "upper": 8,
    "mid": 5,     # 中央
    "middle": 5,
    "center": 5,
    "bottom": 2,  # 下寄せ（既定）
    "lower": 2,
}

# 参考spec の color 語彙 → HEX（PrimaryColour）
_HINT_COLOR_HEX = {
    "white": "#FFFFFF",
    "yellow": "#FFF04D",
    "pink": "#FF6EA7",
    "red": "#FF5555",
    "black": "#111111",
    "orange": "#FFA33A",
    "green": "#7EE07E",
    "blue": "#5EB0FF",
    "cyan": "#54E7F2",
}

# size_class → font_px 倍率（基準 STYLE_BASE_FONTSIZE=76 に対する倍率）。
_HINT_SIZE_SCALE = {
    "small": 0.80,
    "medium": 1.0,
    "med": 1.0,
    "large": 1.30,
    "xl": 1.55,
}


def _resolve_hint_position(hint):
    """telop_style_hint.position → (alignment_code:int, use_top_margin:bool)。

    Alignment 2/5/8 で bottom/middle/top を指定。既定は None（=Base のまま）。
    """
    if not isinstance(hint, dict):
        return None
    pos = (hint.get("position") or "").strip().lower()
    return _HINT_POSITION_ALIGNMENT.get(pos)


def _resolve_hint_color_bgr(hint):
    """telop_style_hint.color → &HBBGGRR& の PrimaryColour override。"""
    if not isinstance(hint, dict):
        return None
    color = (hint.get("color") or "").strip().lower()
    hex_c = _HINT_COLOR_HEX.get(color)
    if not hex_c:
        return None
    try:
        return hex_to_ass_bgr(hex_c)
    except Exception:
        return None


def _resolve_hint_size_scale(hint):
    """telop_style_hint.size_class → font_px 倍率 or None。"""
    if not isinstance(hint, dict):
        return None
    sc = (hint.get("size_class") or "").strip().lower()
    return _HINT_SIZE_SCALE.get(sc)


def build_hint_override_prefix(hint, alignment_default=ALIGNMENT):
    """telop_style_hint から Dialogue 行に前置する ASS override（`{\\anN\\cX\\fsN}`）を組み立てる。

    None を返す場合は「hint 由来の上書きなし」（既定 Style がそのまま使われる）。
    生成 override は Dialogue の `{}` オーバライドブロックの先頭に置き、続く既存
    `{\\fad(...)\\fscxN\\fscyN\\t(...)}` などと連結して1つの override に統合する。

    Returns: str（例 "\\an8\\c&H4DF0FF&\\fs99"）or None
    """
    if not isinstance(hint, dict):
        return None
    parts = []
    align = _resolve_hint_position(hint)
    if align is not None and align != alignment_default:
        parts.append("\\an{}".format(int(align)))
    color = _resolve_hint_color_bgr(hint)
    if color is not None:
        parts.append("\\c{}".format(color))
    return "".join(parts) if parts else None


def hint_font_px(hint, base_font_px):
    """size_class を反映した font_px を返す（該当なしは base_font_px をそのまま）。"""
    scale = _resolve_hint_size_scale(hint)
    if not scale or scale <= 0:
        return int(base_font_px)
    return max(48, int(round(float(base_font_px) * float(scale))))


def build_telop_pieces_from_shots(shots, hook_shot_id=None):
    """shots（各ショットにduration_sec/caption_jpを持つ）から、ショット表示区間に
    同期したtelop断片列を組み立てる。

    ショットの累積時間で out_start/out_end を決める（=render.py がクリップを
    順番に連結する前提と一致させる）。hook_shot_id と一致するショットは
    style="big" にして強調する（最初のショットが無指定ならデフォルトで強調）。

    plan v2 拡張: shot に caption_in_offset_sec / caption_out_offset_sec が
    設定されていれば、テロップの表示開始/終了時刻をショット内のその相対秒に
    合わせる（既定はショット表示区間の先頭〜末尾。SEと字幕が同じ時刻源=
    caption_in_offset_sec を共有し、映像意図と一致する）。未指定なら従来どおり
    ショット全区間を使う（完全後方互換）。
    """
    pieces = []
    cursor = 0.0
    first_id = shots[0]["id"] if shots else None
    for shot in shots or []:
        dur = float(shot.get("duration_sec", 0.0))
        shot_start = cursor
        shot_end = cursor + dur
        cursor = shot_end
        is_big = shot.get("id") == (hook_shot_id or first_id)
        default_font_px = STYLE_BIG_FONTSIZE if is_big else STYLE_BASE_FONTSIZE
        shot_style_hint = shot.get("telop_style_hint") if isinstance(shot.get("telop_style_hint"), dict) else None

        # R4: shot.captions[] があれば各 caption を独立した piece として展開する。
        # 無ければ従来どおり shot.caption_jp を単一 piece として扱う（後方互換）。
        captions_list = shot.get("captions") if isinstance(shot.get("captions"), list) else None
        if captions_list:
            caption_entries = []
            for cap in captions_list:
                text = (cap.get("text") or cap.get("caption_jp") or "").strip()
                if not text:
                    continue
                caption_entries.append({
                    "text": text,
                    "in_off": cap.get("caption_in_offset_sec"),
                    "out_off": cap.get("caption_out_offset_sec"),
                    "hint": (cap.get("telop_style_hint") if isinstance(cap.get("telop_style_hint"), dict) else None) or shot_style_hint,
                })
        else:
            caption_text = (shot.get("caption_jp") or "").strip()
            if not caption_text:
                continue
            caption_entries = [{
                "text": caption_text,
                "in_off": shot.get("caption_in_offset_sec"),
                "out_off": shot.get("caption_out_offset_sec"),
                "hint": shot_style_hint,
            }]

        for j, ent in enumerate(caption_entries):
            cap_in_off = ent["in_off"]
            cap_out_off = ent["out_off"]
            if cap_in_off is not None:
                out_start = shot_start + float(cap_in_off)
            else:
                out_start = shot_start
            if cap_out_off is not None:
                out_end = shot_start + float(cap_out_off)
            else:
                out_end = shot_end
            if out_end < out_start:
                out_end = out_start
            # BUG-55: shot境界を越えないようクランプ
            if out_end > shot_end:
                out_end = shot_end
            if out_start > shot_end:
                out_start = shot_end
            if out_start < shot_start:
                out_start = shot_start

            caption = ent["text"]
            style_hint = ent["hint"]
            # R4 codex-review P2 修正: 各 piece の style（big/base）を先に決めてから、
            # そのスタイルの既定フォントで hint 反映・折り返し・override 判定を行う
            # （複数 caption の hook shot で、2枚目以降は base フォントで扱う）。
            style = "big" if (is_big and j == 0) else "base"
            piece_default_font_px = STYLE_BIG_FONTSIZE if style == "big" else STYLE_BASE_FONTSIZE
            hinted_font_px = hint_font_px(style_hint, piece_default_font_px)
            lines, effective_font_px = wrap_caption_by_width(caption, hinted_font_px)
            if not lines:
                continue
            piece = {
                "out_start": out_start, "out_end": out_end, "lines": lines, "emphasis": [], "style": style,
                "caption": caption,
            }
            if int(effective_font_px) != int(piece_default_font_px):
                piece["font_px_override"] = int(effective_font_px)
            if style_hint:
                piece["telop_style_hint"] = dict(style_hint)
            pieces.append(piece)
    return pieces
