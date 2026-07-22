# -*- coding: utf-8 -*-
"""TTP v2 段別機械QA。

段:
  1. parse  — reference_spec v2 の不変条件検査（純関数）
  2. plan   — plan がスケルトンの写像に忠実か（純関数）
  3. render — 完成mp4に対する「計画SE時刻の音声窓RMSがベースライン+dB以上」検査
              （ffmpeg astats を実測）

CLI:
  python -m qa.ttp_qa <run_work_dir>
  python -m qa.ttp_qa --plan <plan.json> --spec <reference_spec.json>
  python -m qa.ttp_qa --plan <plan.json> --video <output.mp4>

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from pipeline import director


# ---------------------------------------------------------------------------
# 段1: parse — reference_spec v2 の不変条件
# ---------------------------------------------------------------------------

def judge_parse_spec(spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """reference_spec v2 の不変条件を検査する（純関数）。

    - duration_sec > 0
    - cuts が時刻の単調非減少
    - telops[i].start / .end が [0, duration_sec] 内、start <= end
    - sfx_events[i].t が [0, duration_sec] 内
    - hook_end_sec < cta_start_sec（両方あれば）
    """
    errors: List[str] = []
    if not isinstance(spec, dict):
        return False, ["reference_spec が dict ではありません"]

    duration = spec.get("duration_sec")
    if not isinstance(duration, (int, float)) or duration <= 0:
        errors.append("duration_sec が不正: {!r}".format(duration))
        duration = None

    # cuts 単調増加
    cuts = spec.get("cuts") or []
    prev = -1.0
    for i, c in enumerate(cuts):
        if isinstance(c, dict):
            t = c.get("t")
        else:
            t = c
        if not isinstance(t, (int, float)):
            errors.append("cuts[{}].t が数値ではありません: {!r}".format(i, t))
            continue
        tf = float(t)
        if tf < prev - 1e-6:
            errors.append(
                "cuts が時刻の単調非減少に違反: cuts[{}].t={} が直前 {} より小さい".format(
                    i, tf, prev
                )
            )
        if duration is not None and (tf < -1e-6 or tf > float(duration) + 1e-3):
            errors.append(
                "cuts[{}].t={} が [0, duration={}] の範囲外".format(i, tf, duration)
            )
        prev = tf

    # telops 範囲
    for i, tel in enumerate(spec.get("telops") or []):
        if not isinstance(tel, dict):
            errors.append("telops[{}] が dict ではありません".format(i))
            continue
        s = tel.get("start")
        e = tel.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            errors.append("telops[{}] の start/end が数値ではありません: start={!r}, end={!r}".format(i, s, e))
            continue
        if duration is not None:
            if float(s) < -1e-6 or float(s) > float(duration) + 1e-3:
                errors.append("telops[{}].start={} が [0, duration={}] の範囲外".format(i, s, duration))
            if float(e) < -1e-6 or float(e) > float(duration) + 1e-3:
                errors.append("telops[{}].end={} が [0, duration={}] の範囲外".format(i, e, duration))
        if float(e) + 1e-6 < float(s):
            errors.append("telops[{}].end={} が start={} より前です".format(i, e, s))

    # sfx_events 範囲
    for i, ev in enumerate(spec.get("sfx_events") or []):
        if not isinstance(ev, dict):
            errors.append("sfx_events[{}] が dict ではありません".format(i))
            continue
        t = ev.get("t")
        if not isinstance(t, (int, float)):
            errors.append("sfx_events[{}].t が数値ではありません: {!r}".format(i, t))
            continue
        if duration is not None:
            if float(t) < -1e-6 or float(t) > float(duration) + 1e-3:
                errors.append("sfx_events[{}].t={} が [0, duration={}] の範囲外".format(i, t, duration))

    # hook_end < cta_start
    he = spec.get("hook_end_sec")
    cs = spec.get("cta_start_sec")
    if isinstance(he, (int, float)) and isinstance(cs, (int, float)):
        if float(he) >= float(cs):
            errors.append(
                "hook_end_sec={} は cta_start_sec={} より小さい必要があります".format(he, cs)
            )

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# 段2: plan — スケルトン写像に忠実か
# ---------------------------------------------------------------------------

def judge_plan_matches_skeleton(
    plan: Dict[str, Any],
    reference_spec: Dict[str, Any],
    target_duration_sec: Optional[float] = None,
    skeleton_max_shot_sec: Optional[float] = None,
) -> Tuple[bool, List[str]]:
    """plan が reference_spec + target_duration から組み立てたスケルトンに忠実か（純関数）。

    - shot 数一致
    - 各 shot の duration_sec 一致（director._SKELETON_DURATION_MATCH_TOL）
    - 合計尺一致（director._SKELETON_SUM_MATCH_TOL）
    - caption_in_offset_sec / caption_out_offset_sec が対応 shot の duration 内
    - sfx_plan の各 shot_id が plan.shots に存在
    - hook_end_shot_id / cta_start_shot_id が plan.shots に存在（値が入っていれば）
    - plan.shots のいずれも duration_sec が skeleton_max_shot_sec（渡された場合）以下

    target_duration_sec / skeleton_max_shot_sec は plan.meta から自動フォールバックする
    （director が meta.target_duration_sec / meta.skeleton_max_shot_sec を書き込む）。
    """
    errors: List[str] = []
    if not isinstance(plan, dict):
        return False, ["plan が dict ではありません"]

    plan_shots = plan.get("shots") or []
    if not isinstance(plan_shots, list) or not plan_shots:
        return False, ["plan.shots が空です"]

    meta = plan.get("meta") or {}
    # target_duration_sec の解決優先度: 明示引数 > plan.meta > plan.shots 合計
    if target_duration_sec is None:
        m_target = meta.get("target_duration_sec")
        if isinstance(m_target, (int, float)) and m_target > 0:
            target_duration_sec = float(m_target)
        else:
            target_duration_sec = sum(
                float(s.get("duration_sec") or 0.0) for s in plan_shots
            )
    # skeleton_max_shot_sec の解決: 明示引数 > plan.meta.skeleton_max_shot_sec
    if skeleton_max_shot_sec is None:
        m_max = meta.get("skeleton_max_shot_sec")
        if isinstance(m_max, (int, float)) and m_max > 0:
            skeleton_max_shot_sec = float(m_max)

    # スケルトンを組み立てて骨一致を検査（分割設定も合わせる）
    try:
        skeleton = director.build_shot_skeleton(
            reference_spec, float(target_duration_sec),
            max_shot_sec=skeleton_max_shot_sec,
        )
    except Exception as exc:
        return False, ["reference_spec からスケルトンを組み立てられませんでした: {}".format(exc)]

    skel_shots = skeleton.get("shots") or []
    if len(plan_shots) != len(skel_shots):
        errors.append(
            "shots 数がスケルトンと一致しません(plan={}, skeleton={})".format(
                len(plan_shots), len(skel_shots)
            )
        )

    n = min(len(plan_shots), len(skel_shots))
    for i in range(n):
        ps = plan_shots[i]
        ss = skel_shots[i]
        pd = ps.get("duration_sec")
        sd = ss.get("duration_sec")
        if not isinstance(pd, (int, float)) or abs(float(pd) - float(sd)) > director._SKELETON_DURATION_MATCH_TOL:
            errors.append(
                "shots[{}].duration_sec がスケルトンと不一致(plan={}, skeleton={}, 許容±{})".format(
                    i, pd, sd, director._SKELETON_DURATION_MATCH_TOL
                )
            )

    # 合計尺
    total_plan = sum(float(s.get("duration_sec") or 0.0) for s in plan_shots)
    total_skel = sum(float(s.get("duration_sec") or 0.0) for s in skel_shots)
    if abs(total_plan - total_skel) > director._SKELETON_SUM_MATCH_TOL:
        errors.append(
            "shots.duration_sec 合計がスケルトンと不一致(plan={:.3f}, skeleton={:.3f}, 許容±{})".format(
                total_plan, total_skel, director._SKELETON_SUM_MATCH_TOL
            )
        )

    # caption offset が duration 内
    valid_ids = set()
    for i, s in enumerate(plan_shots):
        sid = s.get("id") or "s{}".format(i + 1)
        valid_ids.add(sid)
        dur = s.get("duration_sec")
        cin = s.get("caption_in_offset_sec")
        cout = s.get("caption_out_offset_sec")
        if isinstance(cin, (int, float)) and isinstance(dur, (int, float)):
            if not (-1e-6 <= float(cin) <= float(dur) + 1e-6):
                errors.append(
                    "shots[{}(id={})].caption_in_offset_sec={} が duration_sec={} の範囲外".format(i, sid, cin, dur)
                )
        if isinstance(cout, (int, float)) and isinstance(dur, (int, float)):
            if not (-1e-6 <= float(cout) <= float(dur) + 1e-6):
                errors.append(
                    "shots[{}(id={})].caption_out_offset_sec={} が duration_sec={} の範囲外".format(i, sid, cout, dur)
                )
        if isinstance(cin, (int, float)) and isinstance(cout, (int, float)):
            if float(cout) + 1e-6 < float(cin):
                errors.append(
                    "shots[{}(id={})].caption_out_offset_sec={} が caption_in_offset_sec={} より前".format(i, sid, cout, cin)
                )
        if skeleton_max_shot_sec is not None and isinstance(dur, (int, float)):
            if float(dur) > float(skeleton_max_shot_sec) + 1e-3:
                errors.append(
                    "shots[{}(id={})].duration_sec={} が max_shot_sec={} を超えています".format(
                        i, sid, dur, skeleton_max_shot_sec
                    )
                )

    # sfx_plan.shot_id が実在
    for i, ev in enumerate(plan.get("sfx_plan") or []):
        if not isinstance(ev, dict):
            errors.append("sfx_plan[{}] が dict ではありません".format(i))
            continue
        anc = ev.get("t_anchor") or {}
        sid = anc.get("shot_id")
        if sid not in valid_ids:
            errors.append("sfx_plan[{}].t_anchor.shot_id={!r} が plan.shots に存在しません".format(i, sid))
        # offset_sec が対応 shot の duration 内
        off = anc.get("offset_sec")
        if sid in valid_ids and isinstance(off, (int, float)):
            shot = next((s for s in plan_shots if s.get("id") == sid), None)
            if shot is not None:
                dur = shot.get("duration_sec")
                if isinstance(dur, (int, float)) and not (-1e-6 <= float(off) <= float(dur) + 1e-6):
                    errors.append(
                        "sfx_plan[{}].t_anchor.offset_sec={} が対象shot(id={}) duration={} 範囲外".format(
                            i, off, sid, dur
                        )
                    )

    # hook_end_shot_id / cta_start_shot_id 実在
    for key in ("hook_end_shot_id", "cta_start_shot_id"):
        v = plan.get(key)
        if v is not None and v not in valid_ids:
            errors.append("{}={!r} が plan.shots に存在しません".format(key, v))

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# 段3: render — 計画SE時刻の音声窓RMSがベースライン+dB以上か
# ---------------------------------------------------------------------------

_ASTATS_RMS_RE = re.compile(
    r"lavfi\.astats\.Overall\.RMS_level\s*=\s*(\-?[0-9]+\.?[0-9]*|nan|inf|\-inf)",
    re.IGNORECASE,
)
_ASTATS_PTS_RE = re.compile(
    r"frame:\s*\d+\s+pts:\s*\-?\d+\s+pts_time:\s*(\-?[0-9]+\.?[0-9]*)"
)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def _probe_shot_display_durations(run_dir: str, plan: Dict[str, Any], ffmpeg_bin: str) -> Optional[Dict[str, float]]:
    """work/<run_id>/narration_segments/seg_XXX.wav の実尺から shot_display_durations を復元する。

    run.py の音声主導タイミング同期モードと同じ計算式:
      display_dur = max(seg_duration + SYNC_GAP_SEC(0.25), SYNC_MIN_SHOT_SEC(1.2))
    ここは QA モジュールなので pipeline.render への依存を持たず、定数を直接指定する。
    """
    if not run_dir or not os.path.isdir(run_dir):
        return None
    seg_dir = os.path.join(run_dir, "narration_segments")
    if not os.path.isdir(seg_dir):
        return None
    shots = plan.get("shots") or []
    if not shots:
        return None
    seg_paths = sorted(
        os.path.join(seg_dir, name) for name in os.listdir(seg_dir)
        if name.startswith("seg_") and name.endswith(".wav")
    )
    if len(seg_paths) < len(shots):
        return None
    SYNC_GAP_SEC = 0.25
    SYNC_MIN_SHOT_SEC = 1.2
    displays: Dict[str, float] = {}
    for shot, seg_path in zip(shots, seg_paths):
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-i", seg_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stderr = proc.stderr.decode("utf-8", "replace")
            m = _DURATION_RE.search(stderr)
            if not m:
                continue
            seg_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            continue
        sid = shot.get("id")
        if not sid:
            continue
        displays[sid] = max(seg_dur + SYNC_GAP_SEC, SYNC_MIN_SHOT_SEC)
    return displays if displays else None


def _probe_actual_duration_sec(ffmpeg_bin: str, video_path: str) -> Optional[float]:
    """ffmpeg -i の stderr から Duration をパースして実尺秒を返す(ffprobe 不要)."""
    if not video_path or not os.path.isfile(video_path):
        return None
    try:
        proc = subprocess.run([ffmpeg_bin, "-i", video_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr = proc.stderr.decode("utf-8", "replace")
    except Exception:
        return None
    m = _DURATION_RE.search(stderr)
    if not m:
        return None
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


def _resolve_sfx_absolute_seconds(plan: Dict[str, Any],
                                    actual_duration_sec: Optional[float] = None,
                                    shot_display_durations: Optional[Dict[str, float]] = None) -> List[Tuple[str, float]]:
    """plan.sfx_plan を絶対秒（"family", at_sec）のリストへ解決する。

    BUG-53 修正:
    - shot_display_durations を渡すと、各 shot の実表示尺で境界と offset を per-shot に
      補正する(音声主導タイミング同期モードでは shot 毎に伸縮率が異なるため、global scale
      だと数百 ms のズレが出て 50ms 窓検出が失敗する)。
    - shot_display_durations が無く actual_duration_sec だけあれば、plan.shots 合計尺との
      比で uniform に scale する(従来動作)。どちらも無ければ plan 尺そのまま(後方互換)。
    """
    shots = plan.get("shots") or []
    plan_durations = [float(s.get("duration_sec") or 0.0) for s in shots]
    id_to_idx = {s.get("id"): i for i, s in enumerate(shots) if s.get("id")}

    # per-shot 表示尺の解決
    if shot_display_durations:
        display_durations = [
            float(shot_display_durations.get(s.get("id"), plan_durations[i]))
            for i, s in enumerate(shots)
        ]
    elif actual_duration_sec is not None and sum(plan_durations) > 0:
        gs = float(actual_duration_sec) / sum(plan_durations)
        display_durations = [d * gs for d in plan_durations]
    else:
        display_durations = list(plan_durations)

    # 累積境界（各shotの表示開始秒）
    boundaries: List[float] = []
    cursor = 0.0
    for d in display_durations:
        boundaries.append(cursor)
        cursor += d

    events: List[Tuple[str, float]] = []
    for ev in plan.get("sfx_plan") or []:
        if not isinstance(ev, dict):
            continue
        anc = ev.get("t_anchor") or {}
        sid = anc.get("shot_id")
        if sid not in id_to_idx:
            continue
        i = id_to_idx[sid]
        if i >= len(boundaries):
            continue
        shot_start = boundaries[i]
        # per-shot scale: display_dur / plan_dur。plan_dur が 0 なら 1(等倍)。
        plan_dur = plan_durations[i]
        display_dur = display_durations[i]
        per_shot_scale = (display_dur / plan_dur) if plan_dur > 0 else 1.0
        anchor_type = anc.get("type")
        off = float(anc.get("offset_sec") or 0.0)
        if anchor_type == "caption_in":
            cin = shots[i].get("caption_in_offset_sec")
            at_sec = shot_start + (float(cin or 0.0) + off) * per_shot_scale
        else:
            at_sec = shot_start + off * per_shot_scale
        family = ev.get("family") or "unknown"
        events.append((family, max(0.0, at_sec)))
    events.sort(key=lambda kv: kv[1])
    return events


def _measure_astats_rms_series(ffmpeg_bin: str, video_path: str) -> List[Tuple[float, float]]:
    """ffmpeg astats で (pts_time, rms_dB) の時系列を全編分取り出す。

    metadata=mode=print + astats を使う（reference_v2 と同じ手口）。RMS_level は
    dBFS（無音付近は -inf/nan として出現しうる）。

    BUG-53 修正: 従来 `reset=0.05` は int パースで 0 扱いされ「先頭から累積」の RMS が
    毎フレーム出力されていた(=SE の 20ms 相当のスパイクが cumulative に埋もれる)。
    ffmpeg astats の reset は「N フレームごとに集計をリセット」の整数フレーム数のため、
    reset=1 = 1024samples/48kHz ≈ 21ms 相当の per-frame RMS を返させる。50ms 相当の判定は
    judge_sfx_presence が ±window_sec/2 の複数フレームから max を取ることで実現する。
    """
    cmd = [
        ffmpeg_bin, "-i", video_path,
        "-af", "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = proc.stderr.decode("utf-8", "replace")
    series: List[Tuple[float, float]] = []
    current_t: Optional[float] = None
    for line in stderr.splitlines():
        m_pts = _ASTATS_PTS_RE.search(line)
        if m_pts:
            try:
                current_t = float(m_pts.group(1))
            except Exception:
                current_t = None
            continue
        m_rms = _ASTATS_RMS_RE.search(line)
        if m_rms and current_t is not None:
            raw = m_rms.group(1).lower()
            try:
                if raw in ("nan", "inf", "-inf"):
                    val = float("-inf") if raw != "inf" else float("inf")
                else:
                    val = float(raw)
            except Exception:
                val = float("-inf")
            series.append((current_t, val))
    return series


def _rms_values_in_range(series: List[Tuple[float, float]], t_lo: float, t_hi: float) -> List[float]:
    """t_lo <= pts_time < t_hi の RMS_dB を取り出す（-inf は除外）。"""
    vals: List[float] = []
    for t, v in series:
        if t < t_lo:
            continue
        if t >= t_hi:
            continue
        if v == float("-inf"):
            continue
        vals.append(v)
    return vals


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    m = n // 2
    if n % 2 == 1:
        return s[m]
    return 0.5 * (s[m - 1] + s[m])


_BASELINE_SILENCE_FLOOR_DB = -30.0  # これより低い RMS は "narration silence" とみなし baseline から除外


def judge_sfx_presence(
    events: List[Tuple[str, float]],
    rms_series: List[Tuple[float, float]],
    window_sec: float = 0.5,
    baseline_pad_sec: float = 1.0,
    min_delta_db: float = 3.0,
) -> Tuple[bool, List[str], List[Dict[str, Any]]]:
    """計画SE時刻±window_sec/2 の **RMS peak** が、その前後 baseline_pad_sec のベースライン
    (median) より min_delta_db 以上大きいかを判定する（純関数）。

    実測より: BGM+narration+loudnorm を通した最終出力では平均RMSは全編ほぼ一定になるため、
    RMS 平均を比較しても差が出ない。SE は 50ms スパイクなので、局所窓のピーク値を見る
    ほうが「音が鳴った」の判定として物理的に合っている。

    BUG-53 修正:
    - 閾値を+1.5dB→+3.0dBへ引き上げ、ミックス経路変更(SFXをloudnorm後段でオーバーレイ・
      BGMをSE時刻±0.3sで-4dBダッキング)による可聴性の担保と一致させる。
    - baseline_median は "narration silence"(<= -30dB)を除外して計算する。narration gap は
      本編の実効ベースラインではないため、これを含めるとSEが narration gap に置かれた場合
      「無音より SE の方が小さい」と誤判定される。narration が鳴っている部分の median を
      本来のベースラインとして使う。

    Returns: (ok, errors, per_event_report)
    """
    errors: List[str] = []
    per_event: List[Dict[str, Any]] = []
    if not events:
        return True, [], []
    if not rms_series:
        return False, ["ffmpeg astats から RMS 時系列を取得できませんでした"], []

    max_t = rms_series[-1][0]
    for family, at_sec in events:
        w_lo = max(0.0, at_sec - window_sec / 2)
        w_hi = min(max_t, at_sec + window_sec / 2)
        bl_lo = max(0.0, w_lo - baseline_pad_sec)
        bl_hi = min(max_t, w_hi + baseline_pad_sec)

        win_vals = _rms_values_in_range(rms_series, w_lo, w_hi)
        win_peak = max(win_vals) if win_vals else None
        # ベースライン: 窓の前後 baseline_pad_sec 帯（窓は除外）の median。
        # narration silence(< -30dB) は除外する(BUG-53: gap を baseline に含めない)。
        pre_vals = _rms_values_in_range(rms_series, bl_lo, w_lo)
        post_vals = _rms_values_in_range(rms_series, w_hi, bl_hi)
        bl_all = pre_vals + post_vals
        bl_active = [v for v in bl_all if v > _BASELINE_SILENCE_FLOOR_DB]
        # active が全滅する場合(周辺すべてが silence)は SE 単体との比較になるため
        # ベース値なしとしてスキップ(=ok として扱う)する。
        bl_median = _median(bl_active) if bl_active else None

        item = {
            "family": family,
            "at_sec": round(at_sec, 3),
            "window_peak_db": None if win_peak is None else round(win_peak, 2),
            "baseline_median_db": None if bl_median is None else round(bl_median, 2),
            "delta_db": None,
            "ok": False,
        }
        if win_peak is not None and bl_median is not None:
            delta = win_peak - bl_median
            item["delta_db"] = round(delta, 2)
            item["ok"] = bool(delta >= min_delta_db)
        elif win_peak is not None and bl_median is None:
            # 周辺が silence のみ(narration gap) → SE 単体存在で ok とする。
            # SE 自体が silence 級(<= floor)でないことだけ確認する。
            item["ok"] = bool(win_peak > _BASELINE_SILENCE_FLOOR_DB)
            item["delta_db"] = None if not item["ok"] else 999.0  # sentinel = 周辺silenceでSE単独確認
        per_event.append(item)
        if not item["ok"]:
            errors.append(
                "SE不足: family={} at={:.2f}s peak={} baseline_median={} delta={}dB (要 {}dB以上)".format(
                    family, at_sec, item["window_peak_db"], item["baseline_median_db"],
                    item["delta_db"], min_delta_db,
                )
            )
    return (len(errors) == 0), errors, per_event


# ---------------------------------------------------------------------------
# 統合ランナー
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Optional[Any]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _find_output_mp4_for_run(run_dir: str) -> Optional[str]:
    """work/<run_id>/ のディレクトリ名から output/<run_id>.mp4 を推定する。"""
    if not run_dir:
        return None
    name = os.path.basename(os.path.normpath(run_dir))
    project = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    candidate = os.path.join(project, "output", "{}.mp4".format(name))
    return candidate if os.path.isfile(candidate) else None


def run_ttp_qa(
    plan: Optional[Dict[str, Any]] = None,
    reference_spec: Optional[Dict[str, Any]] = None,
    video_path: Optional[str] = None,
    target_duration_sec: Optional[float] = None,
    ffmpeg_bin: Optional[str] = None,
    skeleton_max_shot_sec: Optional[float] = None,
    shot_display_durations: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """3段QAを実行する。plan / reference_spec / video_path はどれも任意で、
    与えられたものだけ検査する。

    Returns: {"overall_ok": bool, "items": {...}}
    """
    items: Dict[str, Any] = {}

    if reference_spec is not None:
        ok, errs = judge_parse_spec(reference_spec)
        items["parse"] = {"ok": ok, "errors": errs}

    if plan is not None:
        if reference_spec is not None:
            ok, errs = judge_plan_matches_skeleton(
                plan, reference_spec,
                target_duration_sec=target_duration_sec,
                skeleton_max_shot_sec=skeleton_max_shot_sec,
            )
            items["plan"] = {"ok": ok, "errors": errs}
        else:
            items["plan"] = {"ok": True, "errors": [], "note": "reference_spec が無いためスケルトン写像検査はスキップ"}

    if plan is not None and video_path and ffmpeg_bin:
        actual_duration = _probe_actual_duration_sec(ffmpeg_bin, video_path)
        events = _resolve_sfx_absolute_seconds(
            plan, actual_duration_sec=actual_duration,
            shot_display_durations=shot_display_durations,
        )
        # BUG-53 修正: sfx_planner が min_interval_sec で連続SE を間引くため、実 mp4 に載る
        # のは間引き後の subset のみ。QA が plan の全 SE を期待して未再生分まで NG に落とすと
        # 「レンダは仕様どおりだが QA だけ落ちる」偽陰性が出る。実運用と同じ min_interval で
        # 事前に thinning してから astats に照合する。
        try:
            from pipeline import sfx_planner as _sp
            min_interval = _sp.DEFAULT_MIN_INTERVAL_SEC
        except Exception:
            min_interval = 1.5
        thinned: List[Tuple[str, float]] = []
        last_t: Optional[float] = None
        for family, at_sec in events:
            if last_t is not None and (at_sec - last_t) < min_interval:
                continue
            thinned.append((family, at_sec))
            last_t = at_sec
        events = thinned
        try:
            series = _measure_astats_rms_series(ffmpeg_bin, video_path)
        except Exception as exc:
            items["render"] = {"ok": False, "errors": ["astats実行に失敗: {}".format(exc)], "events": []}
        else:
            ok, errs, per_event = judge_sfx_presence(events, series)
            items["render"] = {"ok": ok, "errors": errs, "events": per_event}

    overall_ok = all(v.get("ok") for v in items.values()) if items else False
    return {"overall_ok": overall_ok, "items": items}


def main(argv=None):  # pragma: no cover - CLIエントリ
    parser = argparse.ArgumentParser(
        description="higgsfield-auto-reel TTP v2 段別QA（parse / plan / render）"
    )
    parser.add_argument("run_dir", nargs="?", help="work/<run_id>/ を渡すと plan.json / reference_spec.json / output mp4 を自動発見")
    parser.add_argument("--plan", default=None, help="plan.json のパス")
    parser.add_argument("--spec", default=None, help="reference_spec.json のパス")
    parser.add_argument("--video", default=None, help="完成mp4 のパス（render 段のSE検査を有効化）")
    parser.add_argument("--target-duration", type=float, default=None)
    parser.add_argument("--max-shot-sec", type=float, default=None, help="shot最大尺（超過を検出）")
    args = parser.parse_args(argv)

    plan_path = args.plan
    spec_path = args.spec
    video_path = args.video

    run_dir_arg = None
    if args.run_dir:
        run_dir_arg = args.run_dir
        if plan_path is None:
            candidate = os.path.join(run_dir_arg, "plan.json")
            plan_path = candidate if os.path.isfile(candidate) else None
        if spec_path is None:
            candidate = os.path.join(run_dir_arg, "reference_spec.json")
            spec_path = candidate if os.path.isfile(candidate) else None
        if video_path is None:
            video_path = _find_output_mp4_for_run(run_dir_arg)

    plan = _load_json(plan_path) if plan_path else None
    spec = _load_json(spec_path) if spec_path else None

    from pipeline.config import load_config
    cfg = load_config()
    ffmpeg_bin = cfg.get("ffmpeg_bin")

    # BUG-53 修正: run_dir 経路なら narration_segments から per-shot display_duration を復元
    # して SE 時刻の per-shot scale を実行時と一致させる(global scale だと 100ms 単位でズレる)。
    shot_display_durations = None
    if run_dir_arg and plan is not None and ffmpeg_bin:
        shot_display_durations = _probe_shot_display_durations(run_dir_arg, plan, ffmpeg_bin)

    report = run_ttp_qa(
        plan=plan, reference_spec=spec, video_path=video_path,
        target_duration_sec=args.target_duration,
        ffmpeg_bin=ffmpeg_bin,
        skeleton_max_shot_sec=args.max_shot_sec,
        shot_display_durations=shot_display_durations,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["overall_ok"] else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
