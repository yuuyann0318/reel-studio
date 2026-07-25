# -*- coding: utf-8 -*-
"""FCP7 XML (xmeml) 書き出し。Studio plan(編集結果) -> Premiere Proへ読み込めるxmemlシーケンス。

トラック構成（Phase A 規約）:
  V1: 本編。enabledショットのクリップ列（order順・trim後の実尺でギャップなく連結）。
      名前は `S01/S02...`、Premiereカラーラベル `label2=Iris`。
      タイミングは frame_align_durations() で「フレーム丸め→フレーム基準で累積→秒へ戻す」
      契約を premiere.srt と共有する。trim後の実尺が1フレーム未満のショットはスキップする。
  V2: テロップ用の予約（空トラック）。ユーザーがPremiere側でSRTから起こしたテロップを
      置く場所。ツールからは何も出力しない。
  V3: 演出（赤丸オーバーレイ等）。`profile.emphasis.red_circle` が真なら赤丸プレースホルダを
      先頭ショット区間に enabled=FALSE で配置。名前 `RC01 red_circle`、ラベル `Rose`。
  A1: ナレーション（`narration_path` があるとき全尺配置）。名前 `NAR narration`、ラベル `Forest`。
  A2: BGM（`bgm_path` があるとき全尺配置）。名前 `BGM bgm`、ラベル `Lavender`。gainは
      `plan.bgm.gain_db`、無ければ `profile.audio.bgm_gain_db` を Audio Levels フィルタで反映。
  A3: 効果音（`plan.sfx[]`）。`profile.audio.sfx` が真のときのみ。名前 `SE01 whoosh` 等
      family名（ファイル名先頭語）、ラベル `Caribbean`。

フレームレート正規化: `fps_to_timebase_ntsc(fps)` で `<timebase>` と `<ntsc>` を対応表から決定する
（23.976→(24,TRUE) / 24→(24,FALSE) / 29.97→(30,TRUE) / 30→(30,FALSE) / 59.94→(60,TRUE) /
60→(60,FALSE)、許容誤差±0.01。未知fpsは round(fps)+FALSE+warning）。シーケンスの `<rate>` と
各クリップ/ファイルの `<rate>` の両方に同じ (timebase, ntsc) を適用する。

シーケンスマーカー: `<sequence>` 直下に `<marker>` を出力（Premiereではシーケンスマーカーとして
取り込まれる）。各ショット境界（`S01開始` 等）・plan の hook 終了 / CTA 開始（`hook_end_shot_id`
/ `cta_start_shot_id` から解決可能な場合）・各 SFX の意図（family 名をコメント）を書き出す。

素材の実在チェック: `build_xmeml(..., return_warnings=True)` を指定した呼び出しは
{"xmeml": str, "warnings": list[dict]} を返す。呼び出し側（package.py）が欠品ファイル一覧を
README に書けるようにするための警告経路（XMLの中身は欠品時でも出力する。Premiereでは該当
クリップが「メディアがオフラインです」表示になり、後で再リンクできる）。

決定論: すべてのid/uuidはhash(sha1)ベースで入力から算出する（time.time()/uuid4()等の
非決定要素は一切使わない）。同じ引数から呼べば常にバイト同一のXML文字列を返す。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import urllib.parse
import warnings as _warnings
from pathlib import Path
from xml.sax.saxutils import escape

try:
    from pipeline import telop_style as _telop_style_mod  # style_detail(高解像度テロップ)
except Exception:  # pragma: no cover
    _telop_style_mod = None
try:
    from pipeline import font_map as _font_map_mod  # font_class -> 実在フォント名
except Exception:  # pragma: no cover
    _font_map_mod = None

DEFAULT_FPS = 30
SEQUENCE_WIDTH = 1080
SEQUENCE_HEIGHT = 1920

# SFXは配置基準となるat_secのみplanが持ち、素材自体の尺情報が無いため、Premiere側で
# ユーザーが伸縮させる前提のプレースホルダ尺として使う（短いSE想定の既定値。実尺は
# _probe_media_duration_sec で取得できたときのみ上書きされる）。
SFX_PLACEHOLDER_DURATION_SEC = 1.0

RED_CIRCLE_ASSET_DEFAULT = "assets/overlays/red_circle.png"

_INDENT_UNIT = "  "

# Premiereカラーラベル（FCP7 XML の <labels><label2>...</label2></labels>）。
# トラック役割ごとに色を固定して視認性を上げる。値はPremiereが認識する正式ラベル名。
LABEL_V1 = "Iris"
LABEL_V2 = "Iris"      # Phase B: V2 テロップ(generatoritem)にも色ラベルを付ける
LABEL_V3 = "Rose"
LABEL_A1 = "Forest"
LABEL_A2 = "Lavender"
LABEL_A3 = "Caribbean"

# V2 テロップ(generatoritem) の見た目デフォルト値。Premiere が Text ジェネレータの
# パラメータを完全に honorするとは限らない（FCP7 XML 経路のレガシー扱い）が、
# 「取り込み時にとりあえずそれっぽく出る」ことを狙う。詳細は STYLE_SPEC.md 参照。
CAPTION_FONT_DEFAULT = "Noto Sans JP Black"
CAPTION_FONT_SIZE_DEFAULT = 60  # px 目安（Premiere側で微調整前提）
CAPTION_ALIGN_DEFAULT = "center"
CAPTION_POSITION_DEFAULT = "bottom_safe"   # 下部セーフエリア
CAPTION_MAX_CHARS_DEFAULT = 13
CAPTION_MAX_LINES_DEFAULT = 2

# BGM(A2) の Audio Levels フィルタで出力するキーフレームの標準的な補間種別。
# FCP7 XML の <interpolation> は "Linear"（線形）が Premiere/DaVinci 双方で通る。
_KEYFRAME_INTERPOLATION = "Linear"


# ---------------------------------------------------------------------------
# 小さなヘルパー
# ---------------------------------------------------------------------------

def _esc(text):
    return escape("" if text is None else str(text))


def _ind(depth):
    return _INDENT_UNIT * depth


def _seconds_to_frames(seconds, fps):
    return int(round(float(seconds or 0.0) * fps))


# fps 対応表: (target_fps, timebase, ntsc)
# 許容誤差 ±0.01 で判定し、外れたら round + FALSE にフォールバック（warning）。
_FPS_TABLE = (
    (23.976, 24, "TRUE"),
    (24.0,   24, "FALSE"),
    (29.97,  30, "TRUE"),
    (30.0,   30, "FALSE"),
    (59.94,  60, "TRUE"),
    (60.0,   60, "FALSE"),
)


def fps_to_timebase_ntsc(fps, warnings_out=None):
    """fpsを FCP7 XML の (timebase:int, ntsc:"TRUE"|"FALSE") へ正規化する。

    Premiere/FCP7 の `<rate>` は「整数timebase + NTSCフラグ」で分数フレームレート
    （23.976/29.97/59.94 = timebase*1000/1001）を表す。ここでは対応表で厳密に判定し、
    シーケンスと各クリップ/ファイルの両方に同じ値を適用することで、Premiere取り込み時に
    分数フレームレートが失われない/揺れないようにする。

    Args:
        fps: 数値（整数/小数）または None。None は DEFAULT_FPS 扱い。
        warnings_out: list | None。未知fps時に "unknown_fps" kind の警告を追記する
                      （build_xmeml から橋渡しされる。None なら stderr の DeprecationWarning
                      的な _warnings.warn だけを出す＝後方互換）。

    Returns:
        tuple[int, str]: (timebase, ntsc)。ntsc は "TRUE" / "FALSE" のいずれか。
    """
    if fps is None:
        return (DEFAULT_FPS, "FALSE")
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return (DEFAULT_FPS, "FALSE")
    for target, tb, ntsc in _FPS_TABLE:
        if abs(f - target) <= 0.01:
            return (tb, ntsc)
    fallback_tb = int(round(f)) if f > 0 else DEFAULT_FPS
    _warnings.warn(
        "unknown fps {!r}; falling back to timebase={} ntsc=FALSE".format(fps, fallback_tb),
        stacklevel=2,
    )
    if warnings_out is not None:
        warnings_out.append({
            "kind": "unknown_fps",
            "fps": fps,
            "fallback_timebase": fallback_tb,
        })
    return (fallback_tb, "FALSE")


def frame_align_durations(durations_sec, fps):
    """ショット尺(秒)のリストから「フレーム丸め(round)→フレーム基準で累積→秒へ戻す」
    タイムライン区間を計算する（xmemlとsrtの両方が同一のフレーム量子化契約を共有するための
    共通ヘルパー。premiere.srt.build_srt() から呼ばれる）。

    各ショットの実尺を個別にフレームへ丸めてから整数フレームで累積することで、浮動小数の
    秒をそのまま累積する場合に生じる丸め誤差の蓄積（xmeml側とのタイミングずれ）を防ぐ。

    Args:
        durations_sec: 各ショットのtrim後の実尺(秒)のリスト（呼び出し側の順序どおり連結する
                        前提。負値は呼び出し側でmax(0.0, ...)して渡すこと）。
        fps: フレームレート（整数timebase相当。fps_to_timebase_ntsc の timebase を渡すこと）。

    Returns:
        list[dict]: durations_secと同じ長さ・順序。各要素は
            {"start_frame","end_frame","dur_frames","start_sec","end_sec"}。
            dur_frames が0（1フレーム未満の実尺をroundした結果）の要素も含めて返す
            （ゼロ長ショットのスキップ判定は呼び出し側の責務）。
    """
    fps = float(fps or DEFAULT_FPS)
    cursor_frames = 0
    out = []
    for d in durations_sec:
        dur_frames = int(round(float(d or 0.0) * fps))
        start_frame = cursor_frames
        end_frame = start_frame + dur_frames
        cursor_frames = end_frame
        out.append({
            "start_frame": start_frame,
            "end_frame": end_frame,
            "dur_frames": dur_frames,
            "start_sec": start_frame / fps,
            "end_sec": end_frame / fps,
        })
    return out


def _resolve_path(base_dir, rel_or_abs_path):
    """rel_or_abs_pathをbase_dir基点の絶対パスへ解決する（レキシカル正規化のみ・
    シンボリックリンク解決はしない。実在チェックも行わない=素材が無くてもpathurlは作れる）。
    """
    p = Path(rel_or_abs_path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    return Path(os.path.normpath(str(p)))


def _to_pathurl(abs_path):
    """絶対パスを `file://localhost/...` 形式のpathurlへ変換する（空白等は%エンコード）。"""
    posix_path = str(abs_path)
    quoted = urllib.parse.quote(posix_path, safe="/")
    return "file://localhost" + quoted


def _db_to_level(gain_db):
    """dB値をxmeml Audio Levelsフィルタで使う線形レベル値(0-1換算)へ変換する。"""
    if gain_db is None:
        return None
    return 10.0 ** (float(gain_db) / 20.0)


def _deterministic_id(*parts):
    """partsを連結した文字列からsha1ベースでUUID風文字列(8-4-4-4-12)を決定論的に作る。"""
    joined = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return "{}-{}-{}-{}-{}".format(digest[0:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32])


def _shot_display_name(shot_id, index_zero_based):
    """shot_id からタイムライン表示名 `S01` 形式を作る。

    `s1` や `shot_1` のような既存の shot_id は連番部分を拾って zero-pad する。
    数値化できない場合は index (0-based) + 1 を使う。
    """
    m = re.search(r"(\d+)", str(shot_id or ""))
    n = int(m.group(1)) if m else (index_zero_based + 1)
    return "S{:02d}".format(n)


def _default_ffprobe_bin():
    """`bin/ffprobe` の候補パスを返す（存在しなくても Path を返す。後段でexists()で判定）。"""
    # 遅延importで循環回避（pipeline.config.project_root は本モジュールが遅く読まれても安全）。
    try:
        from pipeline.config import project_root as _pr
        return Path(_pr()) / "bin" / "ffprobe"
    except Exception:
        return Path("bin/ffprobe")


def _probe_media_duration_sec(abs_path, ffprobe_bin=None, timeout_sec=5):
    """絶対パスのメディア尺を `bin/ffprobe` で秒(float)取得する。取れないときは None。

    - ファイル自体が存在しない → None（呼び出し側でフォールバック尺を使う）。
    - ffprobe バイナリが無い/失敗した → None + warning は呼び出し側で付ける。
    決定論性のため、呼び出し側は失敗時のフォールバック尺(SFX_PLACEHOLDER_DURATION_SEC)へ
    落とすことでXMLのビット再現性を担保する（実ファイルが同じなら同じ尺・無ければ既定尺）。

    テストからは `ffprobe_bin` を差し替えるか、モジュール全体を monkeypatch する
    （tests/test_premiere_export.py で行う）。
    """
    try:
        p = Path(str(abs_path))
        if not p.exists() or not p.is_file():
            return None
    except OSError:
        return None
    fp = Path(ffprobe_bin) if ffprobe_bin is not None else _default_ffprobe_bin()
    if not fp.exists():
        return None
    try:
        res = subprocess.run(
            [str(fp), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=timeout_sec, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    stdout = (res.stdout or "").strip()
    if not stdout:
        return None
    try:
        dur = float(stdout)
    except ValueError:
        return None
    if dur <= 0:
        return None
    return dur


def _sfx_family(sfx_file):
    """SFXファイル名から family 名（先頭語）を抜き出す（`whoosh_gen_005.wav` -> `whoosh`）。"""
    if not sfx_file:
        return "sfx"
    stem = Path(str(sfx_file)).stem
    token = re.split(r"[_\-.\s]", stem, maxsplit=1)[0]
    token = re.sub(r"\d+$", "", token)  # 末尾数字を削除
    return (token or "sfx").lower()


# ---------------------------------------------------------------------------
# XML断片ビルダー（すべて「行のリスト」を返す。呼び出し側でjoinする）
# ---------------------------------------------------------------------------

def _rate_lines(timebase, ntsc, depth):
    return [
        _ind(depth) + "<rate>",
        _ind(depth + 1) + "<timebase>{}</timebase>".format(int(timebase)),
        _ind(depth + 1) + "<ntsc>{}</ntsc>".format(ntsc),
        _ind(depth) + "</rate>",
    ]


def _labels_lines(label2, depth):
    if not label2:
        return []
    return [
        _ind(depth) + "<labels>",
        _ind(depth + 1) + "<label2>{}</label2>".format(_esc(label2)),
        _ind(depth) + "</labels>",
    ]


def _file_lines(file_id, name, pathurl, timebase, ntsc, duration_frames, depth, kind):
    """kind: "video" | "audio"。"""
    lines = [_ind(depth) + '<file id="{}">'.format(_esc(file_id))]
    lines.append(_ind(depth + 1) + "<name>{}</name>".format(_esc(name)))
    lines.append(_ind(depth + 1) + "<pathurl>{}</pathurl>".format(_esc(pathurl)))
    lines.extend(_rate_lines(timebase, ntsc, depth + 1))
    lines.append(_ind(depth + 1) + "<duration>{}</duration>".format(duration_frames))
    lines.append(_ind(depth + 1) + "<media>")
    if kind == "video":
        lines.append(_ind(depth + 2) + "<video>")
        lines.append(_ind(depth + 3) + "<samplecharacteristics>")
        lines.append(_ind(depth + 4) + "<width>{}</width>".format(SEQUENCE_WIDTH))
        lines.append(_ind(depth + 4) + "<height>{}</height>".format(SEQUENCE_HEIGHT))
        lines.append(_ind(depth + 3) + "</samplecharacteristics>")
        lines.append(_ind(depth + 2) + "</video>")
    else:
        lines.append(_ind(depth + 2) + "<audio>")
        lines.append(_ind(depth + 3) + "<samplecharacteristics>")
        lines.append(_ind(depth + 4) + "<depth>16</depth>")
        lines.append(_ind(depth + 4) + "<samplerate>48000</samplerate>")
        lines.append(_ind(depth + 3) + "</samplecharacteristics>")
        lines.append(_ind(depth + 2) + "</audio>")
    lines.append(_ind(depth + 1) + "</media>")
    lines.append(_ind(depth) + "</file>")
    return lines


def _audio_level_filter_lines(level, depth, keyframes=None):
    """Audio Levels フィルタの行リスト。

    keyframes: None または list[(when_frame:int, level:float)]。指定時は `<parameter>` に
      `<keyframe>` を列挙して線形補間で音量カーブを表現する（Premiere は Audio Levels の
      パラメータキーフレームを認識してタイムライン上に鉛筆線が引かれる）。
    """
    lines = [
        _ind(depth) + "<filter>",
        _ind(depth + 1) + "<effect>",
        _ind(depth + 2) + "<name>Audio Levels</name>",
        _ind(depth + 2) + "<effectid>audiolevels</effectid>",
        _ind(depth + 2) + "<effectcategory>audiolevels</effectcategory>",
        _ind(depth + 2) + "<effecttype>audiolevels</effecttype>",
        _ind(depth + 2) + "<mediatype>audio</mediatype>",
        _ind(depth + 2) + "<parameter>",
        _ind(depth + 3) + "<name>Level</name>",
        _ind(depth + 3) + "<parameterid>level</parameterid>",
        _ind(depth + 3) + "<value>{:.6f}</value>".format(level),
    ]
    if keyframes:
        for when_frame, kf_level in keyframes:
            lines.append(_ind(depth + 3) + "<keyframe>")
            lines.append(_ind(depth + 4) + "<when>{}</when>".format(int(when_frame)))
            lines.append(_ind(depth + 4) + "<value>{:.6f}</value>".format(float(kf_level)))
            lines.append(_ind(depth + 4) + "<interpolation>{}</interpolation>".format(_KEYFRAME_INTERPOLATION))
            lines.append(_ind(depth + 3) + "</keyframe>")
    lines += [
        _ind(depth + 2) + "</parameter>",
        _ind(depth + 1) + "</effect>",
        _ind(depth) + "</filter>",
    ]
    return lines


def _clipitem_lines(clipitem_id, name, start_frames, end_frames, in_frame, out_frame,
                     timebase, ntsc, file_lines, depth, enabled=True, level=None, label2=None,
                     level_keyframes=None):
    lines = [_ind(depth) + '<clipitem id="{}">'.format(_esc(clipitem_id))]
    lines.append(_ind(depth + 1) + "<name>{}</name>".format(_esc(name)))
    lines.extend(_rate_lines(timebase, ntsc, depth + 1))
    lines.append(_ind(depth + 1) + "<start>{}</start>".format(start_frames))
    lines.append(_ind(depth + 1) + "<end>{}</end>".format(end_frames))
    lines.append(_ind(depth + 1) + "<in>{}</in>".format(in_frame))
    lines.append(_ind(depth + 1) + "<out>{}</out>".format(out_frame))
    if not enabled:
        lines.append(_ind(depth + 1) + "<enabled>FALSE</enabled>")
    lines.extend(_labels_lines(label2, depth + 1))
    lines.extend(file_lines)
    if level is not None or level_keyframes:
        base_level = level if level is not None else 1.0
        lines.extend(_audio_level_filter_lines(base_level, depth + 1, keyframes=level_keyframes))
    lines.append(_ind(depth) + "</clipitem>")
    return lines


_HINT_POSITION_TO_PREMIERE = {
    "top": "top_safe",
    "upper": "top_safe",
    "mid": "center",
    "middle": "center",
    "center": "center",
    "bottom": "bottom_safe",
    "lower": "bottom_safe",
}

_HINT_COLOR_TO_HEX = {
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

_HINT_SIZE_TO_PX = {
    "small": 48,
    "medium": 60,
    "med": 60,
    "large": 78,
    "xl": 92,
}


def _pos_pct_to_premiere(pos_y_pct):
    """pos_y_pct(上端から%) → Premiere 位置語(top_safe/center/bottom_safe)。None は None。"""
    if pos_y_pct is None or not isinstance(pos_y_pct, (int, float)):
        return None
    v = float(pos_y_pct)
    if v < 34.0:
        return _HINT_POSITION_TO_PREMIERE.get("top", "top_safe")
    if v < 67.0:
        return _HINT_POSITION_TO_PREMIERE.get("mid", "center")
    return _HINT_POSITION_TO_PREMIERE.get("bottom", "bottom_safe")


def _resolve_hint_for_xmeml(telop_style_hint, base_size, base_position, base_font=CAPTION_FONT_DEFAULT):
    """telop_style_hint を xmeml パラメータ(size/position/color/font)へ写像する。

    style_detail(高解像度: font_class/weight/fill_color_hex/pos_y_pct/size_pct)があれば最優先で
    使い、無ければ従来の粗い position/color/size_class から解決する（後方互換）。STYLE_SPEC.md 参照。

    Returns: (size:int, position:str, color_hex:str|None, font:str)
    """
    if not isinstance(telop_style_hint, dict):
        return (int(base_size), base_position, None, base_font)

    sd = telop_style_hint.get("style_detail")
    if isinstance(sd, dict) and _telop_style_mod is not None:
        try:
            nsd = _telop_style_mod.normalize_style_detail(sd)
        except Exception:
            nsd = None
        if nsd is not None and _telop_style_mod.is_effective(nsd):
            # フォント: font_class → 実在フォント名。
            font = base_font
            fc = nsd.get("font_class")
            if fc and fc != "unknown" and _font_map_mod is not None:
                try:
                    font = _font_map_mod.resolve_font_family(fc, nsd.get("weight"))
                except Exception:
                    font = base_font
            # サイズ: size_pct(1920基準) → px。無ければ base_size。
            size = int(base_size)
            sp = nsd.get("size_pct")
            if isinstance(sp, (int, float)):
                size = max(24, min(160, int(round(float(sp) / 100.0 * 1920.0 * 0.7))))
            # 位置: pos_y_pct → Premiere 位置語。
            position = _pos_pct_to_premiere(nsd.get("pos_y_pct")) or base_position
            # 色: fill_color_hex を優先。
            color_hex = (nsd.get("fill_color_hex") or "").upper() or None
            if color_hex and not color_hex.startswith("#"):
                color_hex = "#" + color_hex
            return (int(size), position, color_hex, font)

    pos = (telop_style_hint.get("position") or "").strip().lower()
    color = (telop_style_hint.get("color") or "").strip().lower()
    sc = (telop_style_hint.get("size_class") or "").strip().lower()
    size = _HINT_SIZE_TO_PX.get(sc, int(base_size))
    position = _HINT_POSITION_TO_PREMIERE.get(pos, base_position)
    color_hex = _HINT_COLOR_TO_HEX.get(color)
    return (int(size), position, color_hex, base_font)


def _generatoritem_text_lines(gen_id, name, start_frames, end_frames, in_frame, out_frame,
                                 timebase, ntsc, text, wrapped_lines, depth,
                                 font=CAPTION_FONT_DEFAULT, size=CAPTION_FONT_SIZE_DEFAULT,
                                 align=CAPTION_ALIGN_DEFAULT, position=CAPTION_POSITION_DEFAULT,
                                 label2=LABEL_V2, color_hex=None):
    """V2 テロップ用 `<generatoritem>` の行リスト。

    FCP7 XML の Text ジェネレータ表現。Premiere は取り込み時に汎用テキストクリップとして
    扱う。詳細スタイル（縁取り・シャドウ・厳密位置）は Premiere 側の .prtextstyle が正、
    ここでは「文字が入っている」「開始/終了フレームが plan の意図と一致する」ことを担保する。
    """
    display_text = "\n".join(wrapped_lines) if wrapped_lines else (text or "")
    lines = [_ind(depth) + '<generatoritem id="{}">'.format(_esc(gen_id))]
    lines.append(_ind(depth + 1) + "<name>{}</name>".format(_esc(name)))
    lines.extend(_rate_lines(timebase, ntsc, depth + 1))
    lines.append(_ind(depth + 1) + "<start>{}</start>".format(int(start_frames)))
    lines.append(_ind(depth + 1) + "<end>{}</end>".format(int(end_frames)))
    lines.append(_ind(depth + 1) + "<in>{}</in>".format(int(in_frame)))
    lines.append(_ind(depth + 1) + "<out>{}</out>".format(int(out_frame)))
    lines.append(_ind(depth + 1) + "<anamorphic>FALSE</anamorphic>")
    lines.append(_ind(depth + 1) + "<alphatype>straight</alphatype>")
    lines.extend(_labels_lines(label2, depth + 1))
    lines.append(_ind(depth + 1) + "<effect>")
    lines.append(_ind(depth + 2) + "<name>Text</name>")
    lines.append(_ind(depth + 2) + "<effectid>Text</effectid>")
    lines.append(_ind(depth + 2) + "<effectcategory>Text</effectcategory>")
    lines.append(_ind(depth + 2) + "<effecttype>generator</effecttype>")
    lines.append(_ind(depth + 2) + "<mediatype>video</mediatype>")
    # parameter: text
    lines.append(_ind(depth + 2) + "<parameter>")
    lines.append(_ind(depth + 3) + "<parameterid>str</parameterid>")
    lines.append(_ind(depth + 3) + "<name>Text</name>")
    lines.append(_ind(depth + 3) + "<value>{}</value>".format(_esc(display_text)))
    lines.append(_ind(depth + 2) + "</parameter>")
    # parameter: font
    lines.append(_ind(depth + 2) + "<parameter>")
    lines.append(_ind(depth + 3) + "<parameterid>font</parameterid>")
    lines.append(_ind(depth + 3) + "<name>Font</name>")
    lines.append(_ind(depth + 3) + "<value>{}</value>".format(_esc(font)))
    lines.append(_ind(depth + 2) + "</parameter>")
    # parameter: size
    lines.append(_ind(depth + 2) + "<parameter>")
    lines.append(_ind(depth + 3) + "<parameterid>size</parameterid>")
    lines.append(_ind(depth + 3) + "<name>Size</name>")
    lines.append(_ind(depth + 3) + "<value>{}</value>".format(int(size)))
    lines.append(_ind(depth + 2) + "</parameter>")
    # parameter: alignment
    lines.append(_ind(depth + 2) + "<parameter>")
    lines.append(_ind(depth + 3) + "<parameterid>align</parameterid>")
    lines.append(_ind(depth + 3) + "<name>Alignment</name>")
    lines.append(_ind(depth + 3) + "<value>{}</value>".format(_esc(align)))
    lines.append(_ind(depth + 2) + "</parameter>")
    # parameter: position（下部セーフエリア等の意図。Premiere側の座標系と一致しない場合あり）
    lines.append(_ind(depth + 2) + "<parameter>")
    lines.append(_ind(depth + 3) + "<parameterid>position</parameterid>")
    lines.append(_ind(depth + 3) + "<name>Position</name>")
    lines.append(_ind(depth + 3) + "<value>{}</value>".format(_esc(position)))
    lines.append(_ind(depth + 2) + "</parameter>")
    # F9: parameter: fontcolor（telop_style_hint.color → HEX で焼き込む。Premiere は
    # 汎用 Text ジェネレータで <color> パラメータを厳密には解釈しないが、xmeml 上に
    # 意図を残しておくことで .prtextstyle 側で色をあわせるヒントになる）。
    if color_hex:
        lines.append(_ind(depth + 2) + "<parameter>")
        lines.append(_ind(depth + 3) + "<parameterid>fontcolor</parameterid>")
        lines.append(_ind(depth + 3) + "<name>Font Color</name>")
        lines.append(_ind(depth + 3) + "<value>{}</value>".format(_esc(color_hex)))
        lines.append(_ind(depth + 2) + "</parameter>")
    lines.append(_ind(depth + 1) + "</effect>")
    lines.append(_ind(depth) + "</generatoritem>")
    return lines


def _marker_lines(name, comment, in_frame, depth):
    """シーケンス直下 `<marker>` の行リストを返す（Premiereはシーケンスマーカーとして解釈）。"""
    lines = [_ind(depth) + "<marker>"]
    lines.append(_ind(depth + 1) + "<name>{}</name>".format(_esc(name)))
    if comment:
        lines.append(_ind(depth + 1) + "<comment>{}</comment>".format(_esc(comment)))
    lines.append(_ind(depth + 1) + "<in>{}</in>".format(in_frame))
    lines.append(_ind(depth + 1) + "<out>-1</out>")
    lines.append(_ind(depth) + "</marker>")
    return lines


def _track_lines(clipitem_lines_list, depth):
    """clipitem_lines_list: 複数clipitemの行リストのリスト（[] なら空トラック）。"""
    lines = [_ind(depth) + "<track>"]
    for clip_lines in clipitem_lines_list:
        lines.extend(clip_lines)
    lines.append(_ind(depth) + "</track>")
    return lines


# ---------------------------------------------------------------------------
# 各トラックの組み立て
# ---------------------------------------------------------------------------

def _check_missing(role, abs_path, warnings_out):
    """abs_path が実在しなければ warnings_out に1件足す。warnings_out が None ならno-op。"""
    if warnings_out is None:
        return
    try:
        exists = os.path.exists(str(abs_path))
    except OSError:
        exists = False
    if not exists:
        warnings_out.append({
            "kind": "missing_media",
            "role": role,
            "name": Path(str(abs_path)).name,
            "abs_path": str(abs_path),
            "pathurl": _to_pathurl(abs_path),
        })


def _build_v1_clips(enabled_shots, project_dir, timebase, ntsc, depth, warnings_out=None):
    """(clipitem行リストのリスト, total_duration_frames, first_shot_end_frames,
       shot_start_frames) を返す。

    shot_start_frames は enabled_shots 順の (shot_id, display_name, start_frame,
    dur_frames) のタプル列（マーカー用）。

    タイミングは frame_align_durations()（premiere.srtと共有する「フレーム丸め→
    フレーム基準で累積→秒へ戻す」契約）で計算する。trim後の実尺が1フレーム未満
    （round後 dur_frames==0）のショットはV1トラックへclipitemを出力しない
    （ゼロ長ショットガード。0フレームのため後続ショットのタイムライン位置には影響しない）。
    total_duration_frames・first_shot_end_frames は、ゼロ長ショットの有無に関わらず
    常に一致するタイムラインの終端/先頭終端を返す（実際に出力された最初のclipitemの
    終端フレームがfirst_shot_end_framesになる）。
    """
    durations_sec = []
    for shot in enabled_shots:
        trim = shot.get("trim") or {}
        trim_start = float(trim.get("start", 0.0))
        trim_end = float(trim.get("end", trim_start))
        durations_sec.append(max(0.0, trim_end - trim_start))
    aligned = frame_align_durations(durations_sec, timebase)

    clip_lines_list = []
    first_shot_end_frames = 0
    first_emitted = False
    shot_start_frames = []
    for i, shot in enumerate(enabled_shots):
        timing = aligned[i]
        dur_frames = timing["dur_frames"]
        if dur_frames <= 0:
            continue  # ゼロ長ショットガード: 1フレーム未満の実尺はxmeml側もスキップする

        timeline_start = timing["start_frame"]
        timeline_end = timing["end_frame"]

        trim = shot.get("trim") or {}
        trim_start = float(trim.get("start", 0.0))
        in_frame = _seconds_to_frames(trim_start, timebase)
        out_frame = in_frame + dur_frames

        clip_path = shot.get("clip_path") or ""
        if clip_path:
            abs_path = _resolve_path(project_dir, clip_path)
            pathurl = _to_pathurl(abs_path)
            name = Path(clip_path).name
            _check_missing("V1", abs_path, warnings_out)
        else:
            pathurl = ""
            name = shot.get("id") or "clip"

        shot_id = shot.get("id") or "s{}".format(i + 1)
        display_name = _shot_display_name(shot_id, i)
        file_id = "file-v1-{}".format(_deterministic_id("v1-file", shot_id, clip_path))
        clipitem_id = "clipitem-v1-{}".format(_deterministic_id("v1-clip", shot_id, clip_path))

        file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, dur_frames, depth + 2, kind="video")
        clip_lines_list.append(
            _clipitem_lines(
                clipitem_id, display_name, timeline_start, timeline_end, in_frame, out_frame,
                timebase, ntsc, file_lines, depth + 1, enabled=True, label2=LABEL_V1,
            )
        )
        shot_start_frames.append((shot_id, display_name, timeline_start, dur_frames))
        if not first_emitted:
            first_shot_end_frames = timeline_end
            first_emitted = True

    total_duration_frames = aligned[-1]["end_frame"] if aligned else 0
    return clip_lines_list, total_duration_frames, first_shot_end_frames, shot_start_frames


def _build_v3_red_circle(profile, project_dir, timebase, ntsc, first_shot_end_frames, depth,
                          warnings_out=None):
    """profile.emphasis.red_circleが真ならclipitem行リストのリスト([0または1件])を返す。"""
    emphasis = (profile or {}).get("emphasis") or {}
    if not emphasis.get("red_circle") or first_shot_end_frames <= 0:
        return []
    asset_rel = emphasis.get("asset") or RED_CIRCLE_ASSET_DEFAULT
    abs_path = _resolve_path(project_dir, asset_rel)
    pathurl = _to_pathurl(abs_path)
    name = Path(asset_rel).name
    _check_missing("V3", abs_path, warnings_out)

    file_id = "file-v3-{}".format(_deterministic_id("v3-file", asset_rel))
    clipitem_id = "clipitem-v3-{}".format(_deterministic_id("v3-clip", asset_rel))
    file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, first_shot_end_frames, depth + 2, kind="video")
    clip_lines = _clipitem_lines(
        clipitem_id, "RC01 red_circle", 0, first_shot_end_frames, 0, first_shot_end_frames,
        timebase, ntsc, file_lines, depth + 1, enabled=False, label2=LABEL_V3,
    )
    return [clip_lines]


def _build_a1_narration(narration_path, project_dir, timebase, ntsc, total_duration_frames, depth,
                         warnings_out=None):
    if not narration_path or total_duration_frames <= 0:
        return []
    abs_path = _resolve_path(project_dir, narration_path)
    pathurl = _to_pathurl(abs_path)
    name = Path(narration_path).name
    _check_missing("A1", abs_path, warnings_out)
    file_id = "file-a1-{}".format(_deterministic_id("a1-file", narration_path))
    clipitem_id = "clipitem-a1-{}".format(_deterministic_id("a1-clip", narration_path))
    file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, total_duration_frames, depth + 2, kind="audio")
    clip_lines = _clipitem_lines(
        clipitem_id, "NAR narration", 0, total_duration_frames, 0, total_duration_frames,
        timebase, ntsc, file_lines, depth + 1, enabled=True, label2=LABEL_A1,
    )
    return [clip_lines]


def _bgm_curve_keyframes(bgm_curve, base_level, timebase, total_duration_frames):
    """render.compute_edit_enhancement_kwargs() が返す bgm_curve dict を Audio Levels の
    キーフレーム列 [(when_frame, linear_level), ...] へ変換する。

    ffmpegレンダの `build_bgm_curve_volume_filter` と時刻/dB を一致させる:
      - t < hook_end_sec  → hook_gain_db
      - hook_end_sec <= t < cta_start_sec → body_gain_db
      - t >= cta_start_sec → cta_gain_db
      - 各 dip_event 時刻 ± sfx_dip_half_window_sec の窓は上記の値 * (dip_gain_db 相当)

    実装: 各フレームで `_level_at()` を評価し、値が変わったフレーム境界にのみキーフレームを
    書き出す（RLE 圧縮）。値が変わる各フレーム f では「(f-1, 直前セグメントの値), (f, 新値)」の
    ペアを打ってステップ状に切替える（Linear 補間だと Premiere が滑らかに繋いでしまうため）。
    フレーム毎評価にすることで:
      - dip 窓の終端が離れた次キーフレームまで長い ramp になる問題を回避
      - 境界の丸めが「値評価と食い違う」問題を回避
    total_duration_frames はショット総尺のフレーム数（900F=30秒程度が上限想定）で
    O(N) のコストは実用上問題ない。
    Returns: (keyframes_list, initial_level_at_zero)
    """
    if not isinstance(bgm_curve, dict) or total_duration_frames <= 0:
        return [], base_level

    hook_end = float(bgm_curve.get("hook_end_sec", 0.0) or 0.0)
    cta_start = float(bgm_curve.get("cta_start_sec", 0.0) or 0.0)
    hook_lvl = _db_to_level(bgm_curve.get("hook_gain_db", -10))
    body_lvl = _db_to_level(bgm_curve.get("body_gain_db", -14))
    cta_lvl = _db_to_level(bgm_curve.get("cta_gain_db", -12))
    dip_events = bgm_curve.get("dip_events") or []
    dip_half_w = float(bgm_curve.get("sfx_dip_half_window_sec", 0.3))
    dip_db = float(bgm_curve.get("sfx_dip_gain_db", -4.0))
    dip_mult = _db_to_level(dip_db)  # 追加ダッキング倍率(<1.0)

    # dip_events は float 一度パースしておく
    _dips = []
    for dt in dip_events:
        try:
            _dips.append(float(dt))
        except (TypeError, ValueError):
            continue

    def _level_at(t_sec):
        if t_sec < hook_end:
            base = hook_lvl
        elif t_sec >= cta_start:
            base = cta_lvl
        else:
            base = body_lvl
        for dtv in _dips:
            if abs(t_sec - dtv) <= dip_half_w:
                return base * dip_mult
        return base

    tb = float(timebase)
    keyframes = []
    prev_lv = None
    initial = None
    # 各フレームの中心付近で評価するため半フレームの epsilon を足す。
    # これにより「t=0.0 が hook」「t=hook_end 直前は hook、直後は body」といった境界が
    # フレーム単位で正しく分離される（frame f が担当する [f/fps, (f+1)/fps) 区間の内側を評価）。
    eps = 0.5 / tb
    for f in range(int(total_duration_frames) + 1):
        t_at = f / tb + eps
        lv = _level_at(t_at)
        if prev_lv is None:
            keyframes.append((f, lv))
            initial = lv
        elif abs(lv - prev_lv) > 1e-9:
            # 前フレームに step-hold の keyframe を残し、当該フレームに新値を打つ
            if not keyframes or keyframes[-1][0] != f - 1:
                keyframes.append((f - 1, prev_lv))
            keyframes.append((f, lv))
        prev_lv = lv

    if initial is None:
        initial = base_level
    return keyframes, initial


def _build_a2_bgm(bgm_path, plan, profile, project_dir, timebase, ntsc, total_duration_frames, depth,
                   warnings_out=None, bgm_curve=None):
    if not bgm_path or total_duration_frames <= 0:
        return []
    plan_bgm = (plan or {}).get("bgm") or {}
    gain_db = plan_bgm.get("gain_db") if isinstance(plan_bgm, dict) else None
    if gain_db is None:
        gain_db = ((profile or {}).get("audio") or {}).get("bgm_gain_db")
    level = _db_to_level(gain_db)
    if level is None:
        level = 1.0

    keyframes = None
    if bgm_curve is not None:
        keyframes, initial_level = _bgm_curve_keyframes(bgm_curve, level, timebase, total_duration_frames)
        if keyframes:
            # 初期値は最初のキーフレームの値と揃える（Premiere取り込み時の t=0 の値と一致）
            level = initial_level

    abs_path = _resolve_path(project_dir, bgm_path)
    pathurl = _to_pathurl(abs_path)
    name = Path(bgm_path).name
    _check_missing("A2", abs_path, warnings_out)
    file_id = "file-a2-{}".format(_deterministic_id("a2-file", bgm_path))
    clipitem_id = "clipitem-a2-{}".format(_deterministic_id("a2-clip", bgm_path))
    file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, total_duration_frames, depth + 2, kind="audio")
    clip_lines = _clipitem_lines(
        clipitem_id, "BGM bgm", 0, total_duration_frames, 0, total_duration_frames,
        timebase, ntsc, file_lines, depth + 1, enabled=True, level=level, label2=LABEL_A2,
        level_keyframes=keyframes,
    )
    return [clip_lines]


def _build_a3_sfx(plan, profile, project_dir, timebase, ntsc, depth, warnings_out=None,
                    sfx_events=None, ffprobe_bin=None):
    """(clipitem行リストのリスト, sfx_marker_info) を返す。

    sfx_marker_info: [(display_name, family, at_sec_frame, comment), ...] マーカー用。

    - sfx_events (Phase B): resolve_sfx_events() の結果
        [{"path":絶対path, "at_sec":float, "gain_db":float, ...}, ...]
      を優先して使う。plan v2 の sfx_plan → 解決済みイベントをそのまま反映する経路。
      パスから family を抽出し、clip 尺は `bin/ffprobe` で実尺を取得（失敗時は
      SFX_PLACEHOLDER_DURATION_SEC + "sfx_duration_fallback" 警告）。
    - sfx_events が None のときは従来どおり plan["sfx"] を読む（v1 後方互換）。
    どちらの経路でも profile.audio.sfx=false は省略する（従来通り）。
    """
    audio_profile = (profile or {}).get("audio") or {}
    if not audio_profile.get("sfx"):
        return [], []  # profile.audio.sfx=false なら省略
    clip_lines_list = []
    marker_info = []
    seq_no = 0

    if sfx_events is not None:
        # Phase B: 解決済みイベントを使う
        for i, ev in enumerate(sfx_events or []):
            if not isinstance(ev, dict):
                continue
            abs_path_str = ev.get("path")
            if not abs_path_str:
                continue
            seq_no += 1
            at_sec = float(ev.get("at_sec", 0.0) or 0.0)
            # ffmpeg 側は負の at_sec を 0 にクランプする（pipeline.render で amerge が
            # 負秒を無視する契約）。xmeml 側もこれに合わせる。
            if at_sec < 0.0:
                at_sec = 0.0
            start_frames = _seconds_to_frames(at_sec, timebase)

            probed = _probe_media_duration_sec(abs_path_str, ffprobe_bin=ffprobe_bin)
            if probed is None:
                dur_frames = _seconds_to_frames(SFX_PLACEHOLDER_DURATION_SEC, timebase)
                if warnings_out is not None:
                    warnings_out.append({
                        "kind": "sfx_duration_fallback",
                        "role": "A3",
                        "abs_path": str(abs_path_str),
                        "fallback_sec": SFX_PLACEHOLDER_DURATION_SEC,
                    })
            else:
                dur_frames = max(1, int(round(probed * float(timebase))))
            end_frames = start_frames + dur_frames

            gain_db = ev.get("gain_db")
            level = _db_to_level(gain_db)

            abs_path = Path(abs_path_str)
            pathurl = _to_pathurl(abs_path)
            name = abs_path.name
            _check_missing("A3", abs_path, warnings_out)

            family = _sfx_family(name)
            display_name = "SE{:02d} {}".format(seq_no, family)

            file_id = "file-a3-{}".format(_deterministic_id("a3-file", i, abs_path_str, at_sec))
            clipitem_id = "clipitem-a3-{}".format(_deterministic_id("a3-clip", i, abs_path_str, at_sec))
            file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, dur_frames, depth + 2, kind="audio")
            clip_lines_list.append(
                _clipitem_lines(
                    clipitem_id, display_name, start_frames, end_frames, 0, dur_frames,
                    timebase, ntsc, file_lines, depth + 1, enabled=True, level=level, label2=LABEL_A3,
                )
            )
            marker_info.append((display_name, family, start_frames, name))
        return clip_lines_list, marker_info

    # v1 後方互換: plan["sfx"] から
    sfx_list = (plan or {}).get("sfx") or []
    for i, sfx in enumerate(sfx_list):
        sfx_file = sfx.get("file")
        if not sfx_file:
            continue
        seq_no += 1
        at_sec = float(sfx.get("at_sec", 0.0))
        start_frames = _seconds_to_frames(at_sec, timebase)
        dur_frames = _seconds_to_frames(SFX_PLACEHOLDER_DURATION_SEC, timebase)
        end_frames = start_frames + dur_frames
        gain_db = sfx.get("gain_db")
        level = _db_to_level(gain_db)

        rel_path = sfx_file if ("/" in sfx_file or os.path.isabs(sfx_file)) else "assets/sfx/{}".format(sfx_file)
        abs_path = _resolve_path(project_dir, rel_path)
        pathurl = _to_pathurl(abs_path)
        name = Path(sfx_file).name
        _check_missing("A3", abs_path, warnings_out)

        family = _sfx_family(sfx_file)
        display_name = "SE{:02d} {}".format(seq_no, family)

        file_id = "file-a3-{}".format(_deterministic_id("a3-file", i, sfx_file, at_sec))
        clipitem_id = "clipitem-a3-{}".format(_deterministic_id("a3-clip", i, sfx_file, at_sec))
        file_lines = _file_lines(file_id, name, pathurl, timebase, ntsc, dur_frames, depth + 2, kind="audio")
        clip_lines_list.append(
            _clipitem_lines(
                clipitem_id, display_name, start_frames, end_frames, 0, dur_frames,
                timebase, ntsc, file_lines, depth + 1, enabled=True, level=level, label2=LABEL_A3,
            )
        )
        marker_info.append((display_name, family, start_frames, name))
    return clip_lines_list, marker_info


def _build_v2_captions(enabled_shots, shot_display_durations, timebase, ntsc,
                          profile, depth):
    """V2 テロップ用 generatoritem 行リストのリストと、副産物として timeline 用の
    caption 情報 [{shot_id,in_sec,out_sec,text,lines}, ...] を返す。

    - shot_display_durations: enabled_shots と同順の実測表示尺(秒)。テロップの表示区間は
      「ショット表示開始 + caption_in_offset_sec」〜「ショット表示開始 + caption_out_offset_sec」
      で解決する（v1: 未指定なら shot 全区間）。
    - Premiere のフレーム量子化契約(frame_align_durations)と揃えるため、
      各ショット表示開始は「フレーム丸め済みの累積フレーム」から算出する。
    """
    caption_pieces = []
    if not enabled_shots:
        return [], caption_pieces

    # 遅延 import: 循環回避。禁則折り返しは pipeline 側のロジックをそのまま使う。
    try:
        from pipeline.subtitles import wrap_caption_kinsoku
    except Exception:  # pragma: no cover - defensive; pipeline は常に import 可能な想定
        wrap_caption_kinsoku = None  # type: ignore

    telop_profile = (profile or {}).get("telop") or {}
    font = telop_profile.get("font") or CAPTION_FONT_DEFAULT
    align = telop_profile.get("align") or CAPTION_ALIGN_DEFAULT
    position = telop_profile.get("position") or CAPTION_POSITION_DEFAULT
    max_chars = int(telop_profile.get("max_chars", CAPTION_MAX_CHARS_DEFAULT))
    max_lines = int(telop_profile.get("max_lines", CAPTION_MAX_LINES_DEFAULT))

    # 各ショットの「表示尺(秒)」→ frame_align で表示区間を算出
    durations = list(shot_display_durations or [])
    if not durations:
        # フォールバック: trim ベースで再計算（呼び出し側が渡し忘れた場合の安全弁）
        durations = []
        for s in enabled_shots:
            trim = s.get("trim") or {}
            durations.append(max(0.0, float(trim.get("end", 0.0)) - float(trim.get("start", 0.0))))
    aligned = frame_align_durations(durations, timebase)

    clip_lines_list = []
    for i, shot in enumerate(enabled_shots):
        if i >= len(aligned):
            break
        timing = aligned[i]
        if timing["dur_frames"] <= 0:
            continue
        shot_start_sec = timing["start_sec"]
        shot_end_sec = timing["end_sec"]
        shot_id = shot.get("id") or "s{}".format(i + 1)
        shot_style_hint = shot.get("telop_style_hint") if isinstance(shot.get("telop_style_hint"), dict) else None

        # R4: shot.captions[] 対応（複数テロップ）。無ければ従来 caption_jp を単一エントリとして扱う。
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
            caption_text = (shot.get("caption") or shot.get("caption_jp") or "").strip()
            if not caption_text:
                continue
            caption_entries = [{
                "text": caption_text,
                "in_off": shot.get("caption_in_offset_sec"),
                "out_off": shot.get("caption_out_offset_sec"),
                "hint": shot_style_hint,
            }]

        for j, ent in enumerate(caption_entries):
            caption_text = ent["text"]
            cap_in_off = ent["in_off"]
            cap_out_off = ent["out_off"]
            if cap_in_off is not None:
                in_sec = shot_start_sec + float(cap_in_off)
            else:
                in_sec = shot_start_sec
            if cap_out_off is not None:
                out_sec = shot_start_sec + float(cap_out_off)
            else:
                out_sec = shot_end_sec
            if out_sec < in_sec:
                out_sec = in_sec
            if out_sec > shot_end_sec:
                out_sec = shot_end_sec
            if in_sec > shot_end_sec:
                in_sec = shot_end_sec
            if in_sec < shot_start_sec:
                in_sec = shot_start_sec

            start_frames = _seconds_to_frames(in_sec, timebase)
            end_frames = _seconds_to_frames(out_sec, timebase)
            if end_frames <= start_frames:
                end_frames = start_frames + 1
            dur_frames = end_frames - start_frames

            wrapped = []
            if wrap_caption_kinsoku is not None:
                try:
                    wrapped = wrap_caption_kinsoku(caption_text, max_chars=max_chars, max_lines=max_lines) or []
                except Exception:
                    wrapped = [caption_text]
            else:
                wrapped = [caption_text]

            gen_id = "gen-v2-{}".format(_deterministic_id("v2-gen", shot_id, j, caption_text))
            display_name = "TL{:02d}{}".format(i + 1, chr(ord('a') + j) if len(caption_entries) > 1 else "")

            style_hint = ent["hint"]
            effective_size, effective_position, color_hex, effective_font = _resolve_hint_for_xmeml(
                style_hint, CAPTION_FONT_SIZE_DEFAULT, position, base_font=font,
            )

            clip_lines_list.append(
                _generatoritem_text_lines(
                    gen_id, display_name, start_frames, end_frames, 0, dur_frames,
                    timebase, ntsc, caption_text, wrapped, depth + 1,
                    font=effective_font, size=effective_size, align=align, position=effective_position,
                    color_hex=color_hex,
                )
            )
            caption_pieces.append({
                "shot_id": shot_id,
                "caption_index": j,
                "in_sec": start_frames / float(timebase),
                "out_sec": end_frames / float(timebase),
                "in_frame": start_frames,
                "out_frame": end_frames,
                "text": caption_text,
                "lines": wrapped,
            })

    return clip_lines_list, caption_pieces


# ---------------------------------------------------------------------------
# シーケンスマーカー
# ---------------------------------------------------------------------------

def _resolve_beat_frames(plan, shot_start_frames, timebase, shot_display_durations=None):
    """plan.hook_end_shot_id / cta_start_shot_id をシーケンスマーカー用フレームへ解決する。

    Phase B: shot_display_durations が与えられていれば、pipeline.sfx_planner.
    resolve_hook_cta_bounds() で「実測境界」から解決した秒 → フレームに換算する
    （ffmpeg レンダ経路の bgm_curve と時刻源を一致させる）。
    与えられていなければ従来どおり V1 の shot_start_frames から解決する（v1 後方互換）。

    Returns:
        (hook_end_frame:int|None, cta_start_frame:int|None)
    """
    if not isinstance(plan, dict):
        return (None, None)

    if shot_display_durations is not None:
        try:
            from pipeline.sfx_planner import resolve_hook_cta_bounds as _rhcb
            hook_end_sec, cta_start_sec = _rhcb(plan, shot_display_durations)
        except Exception:
            hook_end_sec, cta_start_sec = (None, None)
        hook_frame = _seconds_to_frames(hook_end_sec, timebase) if hook_end_sec is not None else None
        cta_frame = _seconds_to_frames(cta_start_sec, timebase) if cta_start_sec is not None else None
        return (hook_frame, cta_frame)

    if not shot_start_frames:
        return (None, None)
    idx_by_id = {sid: i for i, (sid, _dn, _sf, _df) in enumerate(shot_start_frames)}
    hook_id = plan.get("hook_end_shot_id")
    cta_id = plan.get("cta_start_shot_id")

    hook_frame = None
    if hook_id and hook_id in idx_by_id:
        i = idx_by_id[hook_id]
        _sid, _dn, sf, df = shot_start_frames[i]
        hook_frame = sf + df  # hook終了 = ショット表示終了

    cta_frame = None
    if cta_id and cta_id in idx_by_id:
        i = idx_by_id[cta_id]
        _sid, _dn, sf, _df = shot_start_frames[i]
        cta_frame = sf  # CTA開始 = ショット表示開始

    return (hook_frame, cta_frame)


def _build_sequence_markers(shot_start_frames, sfx_marker_info, plan, timebase, depth,
                              shot_display_durations=None):
    """シーケンス直下 `<marker>` 行リストを返す。

    - ショット境界（`S01開始` 名・shot_id をコメント）
    - hook終了 / CTA開始（plan の shot_id ヒントで解決できたときのみ）
    - 各 SFX の意図（`SE01 whoosh` 名・ファイル名をコメント）
    """
    lines = []
    for sid, display_name, start_frame, _df in shot_start_frames:
        lines.extend(_marker_lines("{}開始".format(display_name), sid, start_frame, depth))

    hook_frame, cta_frame = _resolve_beat_frames(
        plan, shot_start_frames, timebase, shot_display_durations=shot_display_durations,
    )
    if hook_frame is not None:
        lines.extend(_marker_lines("hook_end", "フック終了", hook_frame, depth))
    if cta_frame is not None:
        lines.extend(_marker_lines("cta_start", "CTA開始", cta_frame, depth))

    for display_name, family, at_frame, filename in sfx_marker_info:
        lines.extend(_marker_lines(display_name, "sfx {} ({})".format(family, filename), at_frame, depth))

    return lines


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def build_xmeml(plan, project_dir, narration_path=None, bgm_path=None, profile=None, fps=DEFAULT_FPS,
                 return_warnings=False,
                 shot_display_durations=None, sfx_events=None, bgm_curve=None,
                 emit_captions=False, ffprobe_bin=None, return_timeline=False):
    """Studio plan（編集結果）からFCP7 XML(xmeml)シーケンス全文を生成する。

    Args:
        plan: Studio plan dict（studio/server/projects.py の正規化済みスキーマ想定。
              shots[].{id,order,enabled,clip_path,trim:{start,end}}、bgm、sfx、
              hook_end_shot_id、cta_start_shot_id を参照する）。
        project_dir: shots[].clip_path 等の相対パスを解決する基点ディレクトリ
                     （実運用では project_root()。相対パスの解決のみに使い、実在チェックはしない）。
        narration_path: ナレーション音声ファイルの絶対/相対パス（A1）。Noneなら省略。
        bgm_path: BGM音声ファイルの絶対/相対パス（A2）。Noneなら省略。
        profile: premiere.profile.load_profile() が返すプロファイルdict（emphasis.red_circle /
                 audio.sfx / audio.bgm_gain_db を参照する）。Noneなら空扱い（赤丸なし・sfx省略）。
        fps: フレームレート。`fps_to_timebase_ntsc(fps)` で timebase/ntsc に正規化する
             （23.976/29.97/59.94 は NTSC=TRUE、24/30/60 は NTSC=FALSE）。既定30。
        return_warnings: True のとき戻り値を dict {"xmeml": str, "warnings": list[dict]}
                          にする（False の既定では従来どおり str を返す＝後方互換）。
                          warnings は素材の実在チェックで生成される
                          [{"kind":"missing_media","role":"V1"|"V3"|"A1"|"A2"|"A3",
                            "name":str,"abs_path":str,"pathurl":str}, ...]。

    Returns:
        str: xmeml全文（UTF-8前提のテキスト。同じ引数からは常にバイト同一の文字列を返す）。
        return_warnings=True のときは {"xmeml": str, "warnings": list[dict]}。
    """
    warnings_out = [] if (return_warnings or return_timeline) else None
    timebase, ntsc = fps_to_timebase_ntsc(fps, warnings_out=warnings_out)
    shots = (plan or {}).get("shots") or []
    enabled_shots = sorted(
        [s for s in shots if isinstance(s, dict) and s.get("enabled", True)],
        key=lambda s: s.get("order", 0),
    )

    # 各トラック(<track>)は depth=4（<video>/<audio> depth3 の子）に配置するため、
    # ここで渡す depth=4 を基点に、各builder内部で clipitem=depth+1(5) / file=depth+2(6) を作る
    # （= clipitemはtrackの子、fileはclipitemの子として正しくネストする）。
    v1_clips, total_duration_frames, first_shot_end_frames, shot_start_frames = _build_v1_clips(
        enabled_shots, project_dir, timebase, ntsc, depth=4, warnings_out=warnings_out,
    )
    # V2 テロップ（Phase B）: emit_captions=True のときのみ generatoritem を書き出す。
    # 既存呼び出し（テスト・雑多な import）は emit_captions=False（既定）で従来通り空。
    caption_pieces = []
    if emit_captions:
        v2_clips, caption_pieces = _build_v2_captions(
            enabled_shots, shot_display_durations, timebase, ntsc, profile, depth=4,
        )
    else:
        v2_clips = []
    v3_clips = _build_v3_red_circle(profile, project_dir, timebase, ntsc, first_shot_end_frames,
                                     depth=4, warnings_out=warnings_out)

    a1_clips = _build_a1_narration(narration_path, project_dir, timebase, ntsc,
                                    total_duration_frames, depth=4, warnings_out=warnings_out)
    a2_clips = _build_a2_bgm(bgm_path, plan, profile, project_dir, timebase, ntsc,
                              total_duration_frames, depth=4, warnings_out=warnings_out,
                              bgm_curve=bgm_curve)
    a3_clips, sfx_marker_info = _build_a3_sfx(
        plan, profile, project_dir, timebase, ntsc, depth=4, warnings_out=warnings_out,
        sfx_events=sfx_events, ffprobe_bin=ffprobe_bin,
    )

    marker_lines = _build_sequence_markers(
        shot_start_frames, sfx_marker_info, plan, timebase, depth=2,
        shot_display_durations=shot_display_durations,
    )

    shot_ids = tuple(s.get("id") for s in enabled_shots)
    sequence_uuid = _deterministic_id(
        "xmeml-sequence", str(project_dir), timebase, ntsc, total_duration_frames, shot_ids,
        narration_path, bgm_path, (profile or {}).get("version"),
    )
    sequence_id = "sequence-{}".format(_deterministic_id("sequence-id", sequence_uuid))

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append("<!DOCTYPE xmeml>")
    lines.append('<xmeml version="4">')
    lines.append(_ind(1) + '<sequence id="{}">'.format(_esc(sequence_id)))
    lines.append(_ind(2) + "<uuid>{}</uuid>".format(sequence_uuid))
    lines.append(_ind(2) + "<name>reel_sequence</name>")
    lines.extend(_rate_lines(timebase, ntsc, depth=2))
    lines.append(_ind(2) + "<duration>{}</duration>".format(total_duration_frames))
    lines.append(_ind(2) + "<media>")
    lines.append(_ind(3) + "<video>")
    lines.append(_ind(4) + "<format>")
    lines.append(_ind(5) + "<samplecharacteristics>")
    lines.extend(_rate_lines(timebase, ntsc, depth=6))
    lines.append(_ind(6) + "<width>{}</width>".format(SEQUENCE_WIDTH))
    lines.append(_ind(6) + "<height>{}</height>".format(SEQUENCE_HEIGHT))
    lines.append(_ind(5) + "</samplecharacteristics>")
    lines.append(_ind(4) + "</format>")
    lines.extend(_track_lines(v1_clips, depth=4))
    lines.extend(_track_lines(v2_clips, depth=4))
    lines.extend(_track_lines(v3_clips, depth=4))
    lines.append(_ind(3) + "</video>")
    lines.append(_ind(3) + "<audio>")
    lines.extend(_track_lines(a1_clips, depth=4))
    lines.extend(_track_lines(a2_clips, depth=4))
    lines.extend(_track_lines(a3_clips, depth=4))
    lines.append(_ind(3) + "</audio>")
    lines.append(_ind(2) + "</media>")
    lines.extend(marker_lines)
    lines.append(_ind(1) + "</sequence>")
    lines.append("</xmeml>")

    xmeml_text = "\n".join(lines) + "\n"

    if return_timeline:
        # timeline.json サイドカー用データ（機械可読・後段の verification / debug 用）
        timeline = {
            "version": 2,
            "fps": fps,
            "timebase": timebase,
            "ntsc": ntsc,
            "total_frames": int(total_duration_frames),
            "total_sec": (int(total_duration_frames) / float(timebase)) if timebase else 0.0,
            "shots": [
                {
                    "id": sid, "name": dn,
                    "start_frame": int(sf), "dur_frames": int(df),
                    "start_sec": int(sf) / float(timebase),
                    "end_sec": (int(sf) + int(df)) / float(timebase),
                }
                for (sid, dn, sf, df) in shot_start_frames
            ],
            "captions": caption_pieces,
            "sfx": [
                {
                    "at_frame": int(af), "at_sec": int(af) / float(timebase),
                    "family": fam, "name": name, "display_name": dn,
                }
                for (dn, fam, af, name) in sfx_marker_info
            ],
            "bgm_curve": bgm_curve if isinstance(bgm_curve, dict) else None,
            "markers": {
                "shot_starts": [
                    {"name": "{}開始".format(dn), "shot_id": sid, "at_frame": int(sf),
                     "at_sec": int(sf) / float(timebase)}
                    for (sid, dn, sf, _df) in shot_start_frames
                ],
            },
        }
        # hook_end / cta_start 実測秒（あれば）
        try:
            from pipeline.sfx_planner import resolve_hook_cta_bounds as _rhcb
            if shot_display_durations is not None:
                _hook, _cta = _rhcb(plan, shot_display_durations)
                if _hook is not None:
                    timeline["markers"]["hook_end_sec"] = float(_hook)
                if _cta is not None:
                    timeline["markers"]["cta_start_sec"] = float(_cta)
        except Exception:
            pass
        return {"xmeml": xmeml_text, "warnings": warnings_out or [], "timeline": timeline}
    if return_warnings:
        return {"xmeml": xmeml_text, "warnings": warnings_out or []}
    return xmeml_text
