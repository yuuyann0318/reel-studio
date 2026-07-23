# -*- coding: utf-8 -*-
"""参考動画TTPの「再現度」を数値で測る（F13）。

参考spec (reference_spec v2) と 生成物 (plan v2 + timeline_pieces + sfx_plan) を突き合わせ、
以下の5指標を算出する:

  cut_match:      カット時刻一致率 (±0.5s 窓で二部マッチした F1 スコア)
  telop_iou:      テロップ表示区間の IoU 平均
  telop_style:    テロップスタイル 3属性（position / color / size_class）の一致率
  sfx_placement:  SE 配置一致率（kind 一致 かつ |Δt|≤0.15s）
  camera_move:    カメラワーク一致率（reference の motion / camera_move と生成 motion_preset の対応）

CLI 使い方:
  python -m qa.fidelity --reference-spec /path/to/reference_spec.json \
      --plan /path/to/plan.json [--out /path/to/fidelity.json]

pure Python 3.9+ / stdlib のみ。実映像には触らない（旧仕様spec互換のため欠落フィールドは
"unmeasured" として返し、cut/telop/style は可能な限り測る）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. cut 一致率（F1、±0.5s 窓）
# ---------------------------------------------------------------------------

def _cut_times_from_reference(spec: Dict[str, Any]) -> List[float]:
    """reference_spec.cuts の t 秒列を返す（内部境界のみ。0 と末尾は含めない）。"""
    out: List[float] = []
    for c in (spec or {}).get("cuts") or []:
        if isinstance(c, dict) and isinstance(c.get("t"), (int, float)):
            out.append(float(c["t"]))
        elif isinstance(c, (int, float)):
            out.append(float(c))
    return sorted(t for t in out if t > 0.0)


def _cut_times_from_plan(plan: Dict[str, Any]) -> List[float]:
    """plan.shots の duration_sec 累積から生成側の内部境界秒列を返す（末尾は含めない）。"""
    shots = (plan or {}).get("shots") or []
    boundaries: List[float] = []
    cursor = 0.0
    for i, s in enumerate(shots):
        d = float(s.get("duration_sec") or 0.0)
        cursor += d
        if i < len(shots) - 1:
            boundaries.append(cursor)
    return boundaries


def _scale_cut_times(cuts: List[float], src_total: float, dst_total: float) -> List[float]:
    """参考尺 -> 生成尺 に比例スケール（比較のため座標系を揃える）。"""
    if not cuts or src_total <= 0 or dst_total <= 0:
        return list(cuts)
    ratio = float(dst_total) / float(src_total)
    return [t * ratio for t in cuts]


def _greedy_match_f1(ref: List[float], gen: List[float], tol_sec: float = 0.5) -> Dict[str, float]:
    """±tol_sec 窓での貪欲一部マッチから precision/recall/F1 を返す。

    ref と gen それぞれを最寄りマッチさせて重複採用しない（片方消費）。
    """
    if not ref and not gen:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0}
    if not gen or not ref:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "matched": 0, "ref_count": len(ref), "gen_count": len(gen)}
    remaining = list(range(len(gen)))
    matched = 0
    # ref 側を昇順で走査し、最も近い未消費 gen を選ぶ
    for r in sorted(ref):
        best_j = None
        best_d = None
        for j in remaining:
            d = abs(gen[j] - r)
            if best_d is None or d < best_d:
                best_d = d
                best_j = j
        if best_j is not None and best_d is not None and best_d <= tol_sec:
            matched += 1
            remaining.remove(best_j)
    precision = matched / len(gen) if gen else 0.0
    recall = matched / len(ref) if ref else 0.0
    f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1, "matched": matched,
            "ref_count": len(ref), "gen_count": len(gen)}


# ---------------------------------------------------------------------------
# 2. telop 時間 IoU
# ---------------------------------------------------------------------------

def _telop_intervals_from_reference(spec: Dict[str, Any]) -> List[Tuple[float, float]]:
    out = []
    for tel in (spec or {}).get("telops") or []:
        if isinstance(tel, dict):
            s = tel.get("start"); e = tel.get("end")
            if isinstance(s, (int, float)) and isinstance(e, (int, float)) and float(e) > float(s):
                out.append((float(s), float(e)))
    return out


def _telop_intervals_from_plan(plan: Dict[str, Any]) -> List[Tuple[float, float]]:
    """plan.shots の caption_in/out_offset_sec + ショット累積開始から出力座標の区間を返す。"""
    out = []
    cursor = 0.0
    for s in (plan or {}).get("shots") or []:
        dur = float(s.get("duration_sec") or 0.0)
        shot_start = cursor
        shot_end = cursor + dur
        cursor = shot_end
        if not (s.get("caption_jp") or "").strip():
            continue
        cin = s.get("caption_in_offset_sec")
        cout = s.get("caption_out_offset_sec")
        start = shot_start + (float(cin) if isinstance(cin, (int, float)) else 0.0)
        end = shot_start + (float(cout) if isinstance(cout, (int, float)) else dur)
        if end < start:
            end = start
        end = min(end, shot_end)
        start = max(start, shot_start)
        out.append((start, end))
    return out


def _iou(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    inter = max(0.0, hi - lo)
    union = max(a[1], b[1]) - min(a[0], b[0])
    if union <= 0:
        return 0.0
    return inter / union


def _telop_iou_avg(ref_intervals: List[Tuple[float, float]], gen_intervals: List[Tuple[float, float]]) -> Dict[str, float]:
    if not ref_intervals and not gen_intervals:
        return {"iou_avg": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0}
    if not ref_intervals or not gen_intervals:
        return {"iou_avg": 0.0, "matched": 0, "ref_count": len(ref_intervals), "gen_count": len(gen_intervals)}
    # ref を順に貪欲マッチ
    remaining = list(range(len(gen_intervals)))
    total = 0.0
    matched = 0
    for r in ref_intervals:
        best_j = None
        best_iou = -1.0
        for j in remaining:
            iou = _iou(r, gen_intervals[j])
            if iou > best_iou:
                best_iou = iou
                best_j = j
        if best_j is not None and best_iou > 0:
            total += best_iou
            matched += 1
            remaining.remove(best_j)
    denom = max(len(ref_intervals), len(gen_intervals))
    return {"iou_avg": total / denom if denom else 0.0, "matched": matched,
            "ref_count": len(ref_intervals), "gen_count": len(gen_intervals)}


# ---------------------------------------------------------------------------
# 3. telop スタイル一致率（position/color/size_class 3属性）
# ---------------------------------------------------------------------------

def _hint_from_ref_telop(tel: Dict[str, Any]) -> Dict[str, str]:
    style = tel.get("style") if isinstance(tel.get("style"), dict) else {}
    return {
        "position": (tel.get("position") or "").strip().lower(),
        "color": (style.get("color") or "").strip().lower(),
        "size_class": (style.get("size_class") or "").strip().lower(),
    }


def _hint_from_plan_shot(shot: Dict[str, Any]) -> Optional[Dict[str, str]]:
    h = shot.get("telop_style_hint")
    if not isinstance(h, dict):
        return None
    return {
        "position": (h.get("position") or "").strip().lower(),
        "color": (h.get("color") or "").strip().lower(),
        "size_class": (h.get("size_class") or "").strip().lower(),
    }


def _telop_style_match(spec: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """参考 telops と plan.shots.telop_style_hint を「近い時刻同士」で照合して属性一致率を出す。

    参考の各 telop に対し、生成側で caption を持ち telop_style_hint がある shot のうち
    出力座標時刻が最も近い1つとペアにする。各ペアで position/color/size_class の3属性の
    一致数 / 3 の平均を返す。参考にも生成にも telop が無ければ 1.0（測定不能ではなく合致扱い）。
    """
    ref_telops = [(t, _hint_from_ref_telop(t)) for t in (spec or {}).get("telops") or [] if isinstance(t, dict)]
    if not ref_telops:
        return {"score": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0}
    # 生成側: caption を持ち telop_style_hint も持つ shots
    gen_intervals = _telop_intervals_from_plan(plan)
    shots = (plan or {}).get("shots") or []
    # gen_intervals は caption を持つ順で作られる。同順で shot を対応付ける。
    gen_shot_hints: List[Tuple[Tuple[float, float], Optional[Dict[str, str]]]] = []
    cursor = 0.0
    for s in shots:
        dur = float(s.get("duration_sec") or 0.0)
        shot_start = cursor
        cursor += dur
        cap = (s.get("caption_jp") or "").strip()
        if not cap:
            continue
        cin = s.get("caption_in_offset_sec")
        cout = s.get("caption_out_offset_sec")
        start = shot_start + (float(cin) if isinstance(cin, (int, float)) else 0.0)
        end = shot_start + (float(cout) if isinstance(cout, (int, float)) else dur)
        gen_shot_hints.append(((start, end), _hint_from_plan_shot(s)))

    ref_total = float((spec or {}).get("duration_sec") or 0.0)
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in shots)

    # 参考テロップの中心秒をスケールして生成尺座標にする
    used = set()
    total = 0.0
    matched = 0
    for ref_tel, ref_hint in ref_telops:
        rs = float(ref_tel.get("start") or 0.0)
        re_ = float(ref_tel.get("end") or 0.0)
        mid = (rs + re_) / 2.0
        if ref_total > 0 and gen_total > 0:
            mid_g = mid * gen_total / ref_total
        else:
            mid_g = mid
        # 最も近い未使用 gen shot
        best_i = None
        best_d = None
        for i, ((gs, ge), _hint) in enumerate(gen_shot_hints):
            if i in used:
                continue
            g_mid = (gs + ge) / 2.0
            d = abs(g_mid - mid_g)
            if best_d is None or d < best_d:
                best_d = d
                best_i = i
        if best_i is None:
            continue
        used.add(best_i)
        _, gen_hint = gen_shot_hints[best_i]
        if not gen_hint:
            # skeleton にヒントが伝わっていない = F9 前の状態と同じ = 0/3
            total += 0.0
            matched += 1
            continue
        agree = 0
        for k in ("position", "color", "size_class"):
            rv = ref_hint.get(k) or ""
            gv = gen_hint.get(k) or ""
            if rv and gv and rv == gv:
                agree += 1
            elif not rv and not gv:
                agree += 1  # 両方空: 情報なし=合致扱い
        total += agree / 3.0
        matched += 1
    denom = max(len(ref_telops), matched, 1)
    return {"score": total / denom, "matched": matched,
            "ref_count": len(ref_telops), "gen_count": len(gen_shot_hints)}


# ---------------------------------------------------------------------------
# 4. SE 配置一致率
# ---------------------------------------------------------------------------

_SFX_KIND_TO_FAMILY = {
    "transition": "whoosh",
    "impact": "impact",
    "riser": "riser",
    "pop": "pop",
    "shimmer": "shimmer",
}


def _sfx_events_from_reference(spec: Dict[str, Any]) -> List[Tuple[float, str]]:
    out = []
    for ev in (spec or {}).get("sfx_events") or []:
        if not isinstance(ev, dict):
            continue
        t = ev.get("t"); k = ev.get("kind")
        if isinstance(t, (int, float)) and isinstance(k, str):
            fam = _SFX_KIND_TO_FAMILY.get(k)
            if fam:
                out.append((float(t), fam))
    return out


def _sfx_events_from_plan(plan: Dict[str, Any]) -> List[Tuple[float, str]]:
    shots = (plan or {}).get("shots") or []
    id_to_start = {}
    cursor = 0.0
    for s in shots:
        id_to_start[s.get("id")] = cursor
        cursor += float(s.get("duration_sec") or 0.0)
    out = []
    for ev in (plan or {}).get("sfx_plan") or []:
        if not isinstance(ev, dict):
            continue
        fam = ev.get("family")
        anchor = ev.get("t_anchor") or {}
        sid = anchor.get("shot_id"); off = anchor.get("offset_sec")
        if isinstance(fam, str) and sid in id_to_start and isinstance(off, (int, float)):
            out.append((id_to_start[sid] + float(off), fam))
    return out


def _sfx_placement(spec: Dict[str, Any], plan: Dict[str, Any], tol_sec: float = 0.15) -> Dict[str, Any]:
    ref = _sfx_events_from_reference(spec)
    gen = _sfx_events_from_plan(plan)
    if not ref and not gen:
        return {"score": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0}
    if not ref or not gen:
        return {"score": 0.0, "matched": 0, "ref_count": len(ref), "gen_count": len(gen)}
    # 座標系を揃える
    ref_total = float((spec or {}).get("duration_sec") or 0.0)
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in (plan or {}).get("shots") or [])
    ratio = (gen_total / ref_total) if (ref_total > 0 and gen_total > 0) else 1.0
    ref_scaled = [(t * ratio, fam) for t, fam in ref]
    remaining = list(range(len(gen)))
    matched = 0
    for r_t, r_fam in ref_scaled:
        best_j = None
        best_d = None
        for j in remaining:
            g_t, g_fam = gen[j]
            if g_fam != r_fam:
                continue
            d = abs(g_t - r_t)
            if best_d is None or d < best_d:
                best_d = d
                best_j = j
        if best_j is not None and best_d is not None and best_d <= tol_sec:
            matched += 1
            remaining.remove(best_j)
    denom = max(len(ref_scaled), 1)
    return {"score": matched / denom, "matched": matched,
            "ref_count": len(ref_scaled), "gen_count": len(gen), "tol_sec": tol_sec}


# ---------------------------------------------------------------------------
# 5. camera_move / motion 一致率
# ---------------------------------------------------------------------------

# 参考 motion 語彙（vision 出力の motion / camera_move）→ plan.motion_preset の対応。
_MOTION_MAP = {
    "static": "static",
    "person_talking": "static",
    "pan_left": "pan_left",
    "pan_l": "pan_left",
    "pan_right": "pan_right",
    "pan_r": "pan_right",
    "zoom_in": "zoom_in",
    "zoom_out": "zoom_out",
    "tilt_up": "ken_burns",
    "tilt_down": "ken_burns",
    "handheld": "ken_burns",
    "dolly": "ken_burns",
    "cut_transition": "static",
}


def _camera_move_match(spec: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    shots_ref = (spec or {}).get("shots_ref") or []
    shots = (plan or {}).get("shots") or []
    if not shots_ref or not shots:
        return {"score": 0.0, "matched": 0, "ref_count": len(shots_ref), "gen_count": len(shots)}
    # 参考尺 vs 生成尺で開始時刻ベースに揃える。
    ref_total = float((spec or {}).get("duration_sec") or 0.0)
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in shots)
    ratio = (gen_total / ref_total) if (ref_total > 0 and gen_total > 0) else 1.0
    # gen shot の開始秒
    gen_starts = []
    cursor = 0.0
    for s in shots:
        gen_starts.append((cursor, s))
        cursor += float(s.get("duration_sec") or 0.0)
    used = set()
    total_agree = 0
    total_measured = 0
    for r in shots_ref:
        if not isinstance(r, dict):
            continue
        rs = r.get("start")
        if not isinstance(rs, (int, float)):
            continue
        rs_g = float(rs) * ratio
        # camera_move が無ければ motion にフォールバック
        ref_key = (r.get("camera_move") or r.get("motion") or "").strip().lower()
        if not ref_key:
            continue
        target_preset = _MOTION_MAP.get(ref_key)
        best_i = None
        best_d = None
        for i, (gs, s) in enumerate(gen_starts):
            if i in used:
                continue
            d = abs(gs - rs_g)
            if best_d is None or d < best_d:
                best_d = d
                best_i = i
        if best_i is None:
            continue
        used.add(best_i)
        _, gs = gen_starts[best_i]
        gen_preset = (gs.get("motion_preset") or "").strip().lower()
        total_measured += 1
        if target_preset and gen_preset == target_preset:
            total_agree += 1
    denom = max(total_measured, 1)
    return {"score": total_agree / denom if total_measured else 0.0,
            "matched": total_agree, "measured": total_measured,
            "ref_count": len(shots_ref), "gen_count": len(shots)}


# ---------------------------------------------------------------------------
# 総合スコア
# ---------------------------------------------------------------------------

def compute_fidelity(reference_spec: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """5指標を計算して1つの dict にまとめる。"""
    ref_total = float((reference_spec or {}).get("duration_sec") or 0.0)
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in (plan or {}).get("shots") or [])
    ref_cuts = _cut_times_from_reference(reference_spec)
    ref_cuts_scaled = _scale_cut_times(ref_cuts, ref_total, gen_total) if ref_total > 0 and gen_total > 0 else ref_cuts
    gen_cuts = _cut_times_from_plan(plan)
    cut_res = _greedy_match_f1(ref_cuts_scaled, gen_cuts, tol_sec=0.5)

    ref_telops = _telop_intervals_from_reference(reference_spec)
    gen_telops = _telop_intervals_from_plan(plan)
    # 座標系を揃える（ref を生成尺スケール）
    if ref_total > 0 and gen_total > 0 and ref_total != gen_total:
        ratio = gen_total / ref_total
        ref_telops = [(a * ratio, b * ratio) for a, b in ref_telops]
    telop_iou = _telop_iou_avg(ref_telops, gen_telops)

    telop_style = _telop_style_match(reference_spec, plan)
    sfx = _sfx_placement(reference_spec, plan)
    cam = _camera_move_match(reference_spec, plan)

    summary = {
        "cut_match": cut_res["f1"],
        "telop_iou": telop_iou["iou_avg"],
        "telop_style": telop_style["score"],
        "sfx_placement": sfx["score"],
        "camera_move": cam["score"],
    }
    return {
        "summary": summary,
        "details": {
            "cut_match": cut_res,
            "telop_iou": telop_iou,
            "telop_style": telop_style,
            "sfx_placement": sfx,
            "camera_move": cam,
        },
        "meta": {
            "reference_duration_sec": ref_total,
            "generated_duration_sec": gen_total,
            "reference_cuts_count": len(ref_cuts),
            "generated_shots_count": len((plan or {}).get("shots") or []),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TTP fidelity — 参考動画再現度の5指標を JSON で出力")
    p.add_argument("--reference-spec", required=True, help="reference_spec v2 の JSON パス")
    p.add_argument("--plan", required=True, help="生成 plan v2 の JSON パス")
    p.add_argument("--out", default=None, help="結果 JSON の保存先（省略時は stdout）")
    args = p.parse_args(argv)

    with open(args.reference_spec, "r", encoding="utf-8") as f:
        ref = json.load(f)
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    result = compute_fidelity(ref, plan)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(payload)
        print("[fidelity] wrote {}".format(args.out))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
