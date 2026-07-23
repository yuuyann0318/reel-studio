# -*- coding: utf-8 -*-
"""R2b F8: テロップ帯変化検出によるテロップ時間分解能の向上。

TikTok/Reels のテロップは画面下部（0.6〜0.95 y）または上部（0.05〜0.30 y）帯に
現れることがほとんど。この「帯領域」の輝度差分（フレーム間差の平均RMS）を
ffmpeg で細かい fps 相当で走査し、「テロップが切り替わった瞬間」を秒精度で拾う。

fusion 段が Vision(カットごと2枚のフレーム)から返した spec.telops[i].start/end を、
この change_times にスナップして±0.2秒精度へ補正する。テロップの本文（text）や
style は Vision 側に委ねる（帯変化検出はタイミングのみを担当）。

外部依存: bin/ffmpeg（同梱）。opencv 等の追加依存は不要。
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple


# 帯の Y 位置（相対 0〜1）
_BOTTOM_BAND = (0.60, 0.95)
_TOP_BAND = (0.05, 0.30)

# ffmpeg select frame_dif で使うしきい値。テロップ切替時は帯領域の輝度平均差が
# だいたい 3〜10 程度出る（実測）。小さすぎるとカメラワークの動きで誤検知するため、
# 帯領域を選んでいる。0.02〜0.10 の scdet 相当。
_DEFAULT_SCENE_THRESHOLD = 0.05
# 同一切替点として吸収する時間窓
_DEDUP_WINDOW_SEC = 0.10
# 元 start/end をスナップする最大許容距離
_DEFAULT_SNAP_TOL_SEC = 0.30

_SHOWINFO_PTS_RE = re.compile(r"pts_time:\s*([0-9]+\.?[0-9]*)")


def _detect_band_changes_via_ffmpeg(
    ffmpeg_bin: str,
    video_path: str,
    band: Tuple[float, float],
    threshold: float,
    timeout_sec: int = 90,
) -> List[float]:
    """帯領域を crop してから scdet でシーン変化秒を得る。

    band: (y_ratio_start, y_ratio_end)。0..1 の縦位置範囲。
    scdet は輝度差主体だが、帯を切り出すことで画面全体のカメラ動きより
    テロップの差し替えが支配的な信号になる（実測）。
    """
    y0, y1 = band
    # crop=out_w:out_h:x:y。相対値では ih*y0, ih*(y1-y0) が使える。
    # scdet=t=<threshold>:sc_pass=1 は「シーン変化点だけ通す」オプション（変化フレームのみ後段へ渡る）。
    # -vf 直結で scdet→showinfo。filter_complex+map は scdet が出力ラベルを消費するため NG（実測）。
    vf = "crop=iw:ih*{h}:0:ih*{y},scdet=t={t}:sc_pass=1,showinfo".format(
        h=(y1 - y0), y=y0, t=float(threshold),
    )
    cmd = [
        str(ffmpeg_bin), "-hide_banner", "-nostats", "-i", str(video_path),
        "-vf", vf,
        "-fps_mode", "vfr", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        return []
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    # scdet が使えない古い ffmpeg でも壊れないよう、失敗（exit != 0 かつ output 空）は空返し。
    if proc.returncode != 0 and not _SHOWINFO_PTS_RE.search(stderr):
        return []
    ts: List[float] = []
    for m in _SHOWINFO_PTS_RE.finditer(stderr):
        try:
            ts.append(float(m.group(1)))
        except Exception:
            continue
    ts.sort()
    # 近接点を統合
    dedup = []
    for t in ts:
        if not dedup or (t - dedup[-1]) > _DEDUP_WINDOW_SEC:
            dedup.append(t)
    return dedup


def detect_telop_change_times(
    ffmpeg_bin: str,
    video_path: str,
    threshold: float = _DEFAULT_SCENE_THRESHOLD,
    timeout_sec: int = 90,
    bands: Optional[List[Tuple[float, float]]] = None,
) -> List[float]:
    """上下2つの帯領域のシーン変化秒を合成した昇順リストを返す。

    Returns:
        list[float]: テロップ切替候補秒（近接統合済み・昇順）。
    """
    band_list = list(bands) if bands else [_BOTTOM_BAND, _TOP_BAND]
    combined: List[float] = []
    for band in band_list:
        combined.extend(
            _detect_band_changes_via_ffmpeg(ffmpeg_bin, video_path, band, threshold, timeout_sec=timeout_sec)
        )
    combined.sort()
    dedup: List[float] = []
    for t in combined:
        if not dedup or (t - dedup[-1]) > _DEDUP_WINDOW_SEC:
            dedup.append(round(float(t), 3))
    return dedup


def _snap_time(t: float, change_times: List[float], tol_sec: float) -> Tuple[float, bool]:
    """t を change_times のうち最も近いものに ±tol_sec 以内でスナップする。

    Returns:
        (snapped_t, was_snapped)
    """
    if not change_times:
        return float(t), False
    best = None
    best_d = None
    for ct in change_times:
        d = abs(float(ct) - float(t))
        if best_d is None or d < best_d:
            best_d = d
            best = float(ct)
    if best is not None and best_d is not None and best_d <= tol_sec:
        return float(best), True
    return float(t), False


def refine_telops(
    telops: List[Dict[str, Any]],
    change_times: List[float],
    tol_sec: float = _DEFAULT_SNAP_TOL_SEC,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """spec.telops[].start/end を change_times に ±tol_sec で寄せる（純関数）。

    Returns:
        (refined_telops, stats)  stats={"snapped_start": int, "snapped_end": int, "unchanged": int}
    """
    stats = {"snapped_start": 0, "snapped_end": 0, "unchanged": 0}
    if not telops:
        return list(telops or []), stats
    if not change_times:
        return list(telops), stats
    out: List[Dict[str, Any]] = []
    for tel in telops:
        if not isinstance(tel, dict):
            out.append(tel)
            continue
        s = tel.get("start")
        e = tel.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            out.append(tel)
            continue
        new_s, s_snapped = _snap_time(float(s), change_times, tol_sec)
        new_e, e_snapped = _snap_time(float(e), change_times, tol_sec)
        if new_e < new_s:
            new_e = new_s  # 順序保証
        merged = dict(tel)
        merged["start"] = round(new_s, 3)
        merged["end"] = round(new_e, 3)
        if s_snapped:
            stats["snapped_start"] += 1
        if e_snapped:
            stats["snapped_end"] += 1
        if not (s_snapped or e_snapped):
            stats["unchanged"] += 1
        out.append(merged)
    return out, stats
