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
import urllib.parse
import warnings as _warnings
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_FPS = 30
SEQUENCE_WIDTH = 1080
SEQUENCE_HEIGHT = 1920

# SFXは配置基準となるat_secのみplanが持ち、素材自体の尺情報が無いため、Premiere側で
# ユーザーが伸縮させる前提のプレースホルダ尺として使う（短いSE想定の既定値）。
SFX_PLACEHOLDER_DURATION_SEC = 1.0

RED_CIRCLE_ASSET_DEFAULT = "assets/overlays/red_circle.png"

_INDENT_UNIT = "  "

# Premiereカラーラベル（FCP7 XML の <labels><label2>...</label2></labels>）。
# トラック役割ごとに色を固定して視認性を上げる。値はPremiereが認識する正式ラベル名。
LABEL_V1 = "Iris"
LABEL_V3 = "Rose"
LABEL_A1 = "Forest"
LABEL_A2 = "Lavender"
LABEL_A3 = "Caribbean"


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


def fps_to_timebase_ntsc(fps):
    """fpsを FCP7 XML の (timebase:int, ntsc:"TRUE"|"FALSE") へ正規化する。

    Premiere/FCP7 の `<rate>` は「整数timebase + NTSCフラグ」で分数フレームレート
    （23.976/29.97/59.94 = timebase*1000/1001）を表す。ここでは対応表で厳密に判定し、
    シーケンスと各クリップ/ファイルの両方に同じ値を適用することで、Premiere取り込み時に
    分数フレームレートが失われない/揺れないようにする。

    Args:
        fps: 数値（整数/小数）または None。None は DEFAULT_FPS 扱い。

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


def _audio_level_filter_lines(level, depth):
    return [
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
        _ind(depth + 2) + "</parameter>",
        _ind(depth + 1) + "</effect>",
        _ind(depth) + "</filter>",
    ]


def _clipitem_lines(clipitem_id, name, start_frames, end_frames, in_frame, out_frame,
                     timebase, ntsc, file_lines, depth, enabled=True, level=None, label2=None):
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
    if level is not None:
        lines.extend(_audio_level_filter_lines(level, depth + 1))
    lines.append(_ind(depth) + "</clipitem>")
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


def _build_a2_bgm(bgm_path, plan, profile, project_dir, timebase, ntsc, total_duration_frames, depth,
                   warnings_out=None):
    if not bgm_path or total_duration_frames <= 0:
        return []
    plan_bgm = (plan or {}).get("bgm") or {}
    gain_db = plan_bgm.get("gain_db") if isinstance(plan_bgm, dict) else None
    if gain_db is None:
        gain_db = ((profile or {}).get("audio") or {}).get("bgm_gain_db")
    level = _db_to_level(gain_db)

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
    )
    return [clip_lines]


def _build_a3_sfx(plan, profile, project_dir, timebase, ntsc, depth, warnings_out=None):
    """(clipitem行リストのリスト, sfx_marker_info) を返す。

    sfx_marker_info: [(display_name, family, at_sec_frame, comment), ...] マーカー用。
    """
    audio_profile = (profile or {}).get("audio") or {}
    if not audio_profile.get("sfx"):
        return [], []  # profile.audio.sfx=false なら省略
    sfx_list = (plan or {}).get("sfx") or []
    clip_lines_list = []
    marker_info = []
    seq_no = 0
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


# ---------------------------------------------------------------------------
# シーケンスマーカー
# ---------------------------------------------------------------------------

def _resolve_beat_frames(plan, shot_start_frames, timebase):
    """plan.hook_end_shot_id / cta_start_shot_id をシーケンスマーカー用フレームへ解決する。

    shot_start_frames: _build_v1_clips が返した (shot_id, display_name, start_frame, dur_frames)。
    Returns:
        (hook_end_frame:int|None, cta_start_frame:int|None)
    """
    if not isinstance(plan, dict) or not shot_start_frames:
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


def _build_sequence_markers(shot_start_frames, sfx_marker_info, plan, timebase, depth):
    """シーケンス直下 `<marker>` 行リストを返す。

    - ショット境界（`S01開始` 名・shot_id をコメント）
    - hook終了 / CTA開始（plan の shot_id ヒントで解決できたときのみ）
    - 各 SFX の意図（`SE01 whoosh` 名・ファイル名をコメント）
    """
    lines = []
    for sid, display_name, start_frame, _df in shot_start_frames:
        lines.extend(_marker_lines("{}開始".format(display_name), sid, start_frame, depth))

    hook_frame, cta_frame = _resolve_beat_frames(plan, shot_start_frames, timebase)
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
                 return_warnings=False):
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
    timebase, ntsc = fps_to_timebase_ntsc(fps)
    shots = (plan or {}).get("shots") or []
    enabled_shots = sorted(
        [s for s in shots if isinstance(s, dict) and s.get("enabled", True)],
        key=lambda s: s.get("order", 0),
    )

    warnings_out = [] if return_warnings else None

    # 各トラック(<track>)は depth=4（<video>/<audio> depth3 の子）に配置するため、
    # ここで渡す depth=4 を基点に、各builder内部で clipitem=depth+1(5) / file=depth+2(6) を作る
    # （= clipitemはtrackの子、fileはclipitemの子として正しくネストする）。
    v1_clips, total_duration_frames, first_shot_end_frames, shot_start_frames = _build_v1_clips(
        enabled_shots, project_dir, timebase, ntsc, depth=4, warnings_out=warnings_out,
    )
    v2_clips = []  # 予約(空トラック)
    v3_clips = _build_v3_red_circle(profile, project_dir, timebase, ntsc, first_shot_end_frames,
                                     depth=4, warnings_out=warnings_out)

    a1_clips = _build_a1_narration(narration_path, project_dir, timebase, ntsc,
                                    total_duration_frames, depth=4, warnings_out=warnings_out)
    a2_clips = _build_a2_bgm(bgm_path, plan, profile, project_dir, timebase, ntsc,
                              total_duration_frames, depth=4, warnings_out=warnings_out)
    a3_clips, sfx_marker_info = _build_a3_sfx(
        plan, profile, project_dir, timebase, ntsc, depth=4, warnings_out=warnings_out,
    )

    marker_lines = _build_sequence_markers(shot_start_frames, sfx_marker_info, plan, timebase, depth=2)

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
    if return_warnings:
        return {"xmeml": xmeml_text, "warnings": warnings_out or []}
    return xmeml_text
