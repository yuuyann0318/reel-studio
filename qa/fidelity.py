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


def _ref_shot_boundaries(spec: Dict[str, Any]) -> List[float]:
    """spec.cuts（優先）/ shots_ref から参考尺の shot 境界配列を返す ([0, ..., duration_sec])。"""
    duration = float((spec or {}).get("duration_sec") or 0.0)
    cuts = (spec or {}).get("cuts") or []
    if cuts:
        ts = []
        for c in cuts:
            if isinstance(c, dict) and isinstance(c.get("t"), (int, float)):
                ts.append(float(c["t"]))
            elif isinstance(c, (int, float)):
                ts.append(float(c))
        boundaries = [0.0] + sorted(t for t in ts if 0.0 < t < duration + 1e-3) + [duration]
    else:
        shots_ref = (spec or {}).get("shots_ref") or []
        boundaries = [0.0]
        for s in shots_ref:
            end = s.get("end") if isinstance(s, dict) else None
            if isinstance(end, (int, float)) and end > boundaries[-1]:
                boundaries.append(float(end))
        if not boundaries or boundaries[-1] < duration - 1e-3:
            boundaries.append(duration)
    # 重複掃除
    dedup = []
    for b in boundaries:
        if not dedup or b > dedup[-1] + 1e-3:
            dedup.append(round(b, 6))
    if len(dedup) < 2:
        dedup = [0.0, duration if duration > 0 else 1.0]
    return dedup


def _scale_ref_intervals_piecewise_or_linear(
    ref_intervals: List[Tuple[float, float]],
    spec: Dict[str, Any],
    plan: Dict[str, Any],
    ref_total: float,
    gen_total: float,
) -> List[Tuple[float, float]]:
    """R3: 参考の (start, end) を「参考尺→生成尺」へ写像する。

    優先順位:
      1. plan.meta.shot_ref_ranges (skeleton が持ち込む正確な写像) を使う piecewise
         — boundary merge / max_shot_sec 分割にも対応（P2 修正）。
      2. plan.shots 数 == spec の shot 数 なら、spec のカット境界と plan の shot 尺で piecewise。
      3. どちらも不可なら線形一括スケール（後方互換）。
    """
    if not ref_intervals:
        return ref_intervals
    plan_shots = (plan or {}).get("shots") or []
    if not plan_shots:
        return ref_intervals
    # target 境界（plan.shots 累積尺）
    tgt_bounds = [0.0]
    for s in plan_shots:
        tgt_bounds.append(tgt_bounds[-1] + float(s.get("duration_sec") or 0.0))

    # 1) plan.meta.shot_ref_ranges を使う piecewise（skeleton が正確な対応を持ち込むケース）
    meta = (plan or {}).get("meta") or {}
    shot_ref_ranges = meta.get("shot_ref_ranges")
    if isinstance(shot_ref_ranges, list) and len(shot_ref_ranges) == len(plan_shots):
        try:
            ref_bounds_extended = [float(shot_ref_ranges[0][0])] + [
                float(rng[1]) for rng in shot_ref_ranges
            ]
        except (TypeError, IndexError):
            ref_bounds_extended = None
        if ref_bounds_extended is not None and len(ref_bounds_extended) == len(plan_shots) + 1:
            return _piecewise_map_intervals(ref_intervals, ref_bounds_extended, tgt_bounds, ref_total, gen_total)

    # 2) spec のカット境界と plan の shot 数の一致（skeleton meta が無い後方互換パス）
    ref_bounds = _ref_shot_boundaries(spec)
    if len(plan_shots) == len(ref_bounds) - 1 and ref_total > 0 and gen_total > 0:
        return _piecewise_map_intervals(ref_intervals, ref_bounds, tgt_bounds, ref_total, gen_total)

    # 3) 一致しない場合は線形一括スケール（後方互換）
    if ref_total > 0 and gen_total > 0 and ref_total != gen_total:
        ratio = gen_total / ref_total
        return [(a * ratio, b * ratio) for a, b in ref_intervals]
    return ref_intervals


def _piecewise_map_intervals(
    intervals: List[Tuple[float, float]],
    ref_bounds: List[float],
    tgt_bounds: List[float],
    ref_total: float,
    gen_total: float,
) -> List[Tuple[float, float]]:
    """区分線形写像: ref_bounds[i] -> tgt_bounds[i]。範囲外は線形フォールバック。"""
    n = len(ref_bounds) - 1

    def _map(t: float) -> float:
        for i in range(n):
            lo, hi = ref_bounds[i], ref_bounds[i + 1]
            if lo - 1e-6 <= t <= hi + 1e-6:
                seg = max(1e-6, hi - lo)
                rel = (t - lo) / seg
                tlo, thi = tgt_bounds[i], tgt_bounds[i + 1]
                return tlo + rel * (thi - tlo)
        if ref_total > 0 and gen_total > 0:
            return t * (gen_total / ref_total)
        return t

    return [(_map(a), _map(b)) for a, b in intervals]


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


def _iou_matrix(refs: List[Tuple[float, float]], gens: List[Tuple[float, float]]) -> List[List[float]]:
    return [[_iou(r, g) for g in gens] for r in refs]


def _max_weight_matching_bruteforce(weights: List[List[float]]) -> List[Tuple[int, int]]:
    """総当たりで重み和最大の完全（片方消費）マッチングを返す。n<=8 想定。

    weights[i][j] = i(ref) と j(gen) のペアの重み。片方消費のため
    min(len(refs), len(gens)) 個のペアを選ぶ。O((max)!) なので小さいnのみ。
    """
    m = len(weights)
    if m == 0:
        return []
    n = len(weights[0]) if weights else 0
    if n == 0:
        return []
    # ref を順に、gen 側のどの列を消費するかを permutation で探る。
    # 選ばない ref もあり得るため、gen 側から m 個を選ぶ順列（P(n, m) 通り）を列挙。
    # m <= n を保証するため、必要なら refs と gens を swap して呼び出す（呼び出し側で対応）。
    from itertools import permutations
    best_sum = -1.0
    best_pairs: List[Tuple[int, int]] = []
    if m > n:
        # gens が足りない: gens は全部使い、ref 側の一部だけ選ぶ。
        # ref 側 n 個の組合せ × それらへの gens の順列。
        from itertools import combinations
        for ref_sel in combinations(range(m), n):
            for perm in permutations(range(n)):
                s = 0.0
                pairs = []
                for k, i in enumerate(ref_sel):
                    j = perm[k]
                    w = weights[i][j]
                    if w <= 0:
                        continue
                    s += w
                    pairs.append((i, j))
                if s > best_sum:
                    best_sum = s
                    best_pairs = pairs
    else:
        for perm in permutations(range(n), m):
            s = 0.0
            pairs = []
            for i in range(m):
                j = perm[i]
                w = weights[i][j]
                if w <= 0:
                    continue
                s += w
                pairs.append((i, j))
            if s > best_sum:
                best_sum = s
                best_pairs = pairs
    return best_pairs


def _max_weight_matching_greedy(weights: List[List[float]]) -> List[Tuple[int, int]]:
    """weights 最大順に貪欲にペアを取る。行/列を1回のみ消費。大きな n 用。"""
    if not weights or not weights[0]:
        return []
    entries: List[Tuple[float, int, int]] = []
    for i, row in enumerate(weights):
        for j, w in enumerate(row):
            if w > 0:
                entries.append((w, i, j))
    entries.sort(key=lambda x: -x[0])
    used_i: set = set()
    used_j: set = set()
    pairs: List[Tuple[int, int]] = []
    for w, i, j in entries:
        if i in used_i or j in used_j:
            continue
        pairs.append((i, j))
        used_i.add(i)
        used_j.add(j)
    return pairs


def _telop_iou_avg(ref_intervals: List[Tuple[float, float]], gen_intervals: List[Tuple[float, float]]) -> Dict[str, float]:
    """R2a: telop_iou マッチングを「貪欲(先頭ref優先)」から「最大重みマッチング」へ改善。

    n=len(refs)+len(gens) が 10 以下なら総当たりで最適マッチング、
    それ以上なら重み降順の貪欲でO(n^2 log n)で近似する。
    """
    if not ref_intervals and not gen_intervals:
        return {"iou_avg": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0}
    if not ref_intervals or not gen_intervals:
        return {"iou_avg": 0.0, "matched": 0, "ref_count": len(ref_intervals), "gen_count": len(gen_intervals)}
    weights = _iou_matrix(ref_intervals, gen_intervals)
    total_n = len(ref_intervals) + len(gen_intervals)
    if total_n <= 10:
        pairs = _max_weight_matching_bruteforce(weights)
    else:
        pairs = _max_weight_matching_greedy(weights)
    total = sum(weights[i][j] for i, j in pairs)
    matched = len(pairs)
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
    """参考の全 sfx_events を返す（旧基準・raw）。kind→family マップに乗るもののみ。"""
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


def _salient_sfx_events_from_reference(
    spec: Dict[str, Any], gen_total_sec: float,
) -> List[Tuple[float, str]]:
    """R3: 参考 sfx_events から「顕著オンセット」だけを取り出す（新基準）。

    plan 側の SE 枠 = floor(target/2.5) と同数程度を上限に、
    pipeline.director.select_salient_onsets の統一実装で抽出する。
    ここでは gen_total_sec（生成尺）をベースに上限を決めることで、
    plan.sfx_plan と denom を揃える（sfx_placement の denom = |salient| になる）。
    """
    from pipeline.director import select_salient_onsets, _SKELETON_SFX_SEC_DIVISOR
    max_count = max(1, int(gen_total_sec / _SKELETON_SFX_SEC_DIVISOR))
    out = []
    for ev in select_salient_onsets((spec or {}).get("sfx_events") or [], max_count=max_count):
        fam = _SFX_KIND_TO_FAMILY.get(ev["kind"])
        if fam:
            out.append((float(ev["t"]), fam))
    return out


def _sfx_events_from_plan(plan: Dict[str, Any]) -> List[Tuple[float, str]]:
    """plan.sfx_plan の各イベントを絶対時刻（生成尺内の秒）に解決して返す。

    R3 codex-review 修正: anchor_type="caption_in" は
    pipeline.sfx_planner._resolve_anchor_absolute_sec と同じ規約で
    `shot_start + caption_in_offset_sec + offset_sec` として解決する
    （旧実装は type によらず `shot_start + offset_sec` としており、
     caption_in アンカーの絶対時刻が実レンダーとズレる P1 bug があった）。
    """
    shots = (plan or {}).get("shots") or []
    id_to_start: Dict[Any, float] = {}
    id_to_caption_in: Dict[Any, float] = {}
    cursor = 0.0
    for s in shots:
        sid = s.get("id")
        id_to_start[sid] = cursor
        ci = s.get("caption_in_offset_sec")
        id_to_caption_in[sid] = float(ci) if isinstance(ci, (int, float)) else 0.0
        cursor += float(s.get("duration_sec") or 0.0)
    out = []
    for ev in (plan or {}).get("sfx_plan") or []:
        if not isinstance(ev, dict):
            continue
        fam = ev.get("family")
        anchor = ev.get("t_anchor") or {}
        sid = anchor.get("shot_id"); off = anchor.get("offset_sec")
        atype = anchor.get("type")
        if not isinstance(fam, str) or sid not in id_to_start or not isinstance(off, (int, float)):
            continue
        base = id_to_start[sid]
        if atype == "caption_in":
            base += id_to_caption_in.get(sid, 0.0)
        out.append((base + float(off), fam))
    return out


def _sfx_placement_score(
    ref_events: List[Tuple[float, str]],
    gen_events: List[Tuple[float, str]],
    tol_sec: float,
    use_family_match: bool = True,
    use_f1: bool = False,
) -> Dict[str, Any]:
    """SE 配置一致の共通スコアラー。

    tol_sec 窓内で貪欲二部マッチ。use_family_match=True のときのみ family 一致を要求。
    use_f1=True なら F1（precision/recall の調和平均）で返す（denom = ref+gen 両方を意識）。
    use_f1=False なら matched / len(ref) を返す（旧仕様の raw スコア）。
    """
    if not ref_events and not gen_events:
        return {"score": 1.0, "matched": 0, "ref_count": 0, "gen_count": 0, "tol_sec": tol_sec}
    if not ref_events or not gen_events:
        if use_f1:
            return {"score": 0.0, "matched": 0, "precision": 0.0, "recall": 0.0,
                    "ref_count": len(ref_events), "gen_count": len(gen_events), "tol_sec": tol_sec}
        return {"score": 0.0, "matched": 0, "ref_count": len(ref_events),
                "gen_count": len(gen_events), "tol_sec": tol_sec}
    remaining = list(range(len(gen_events)))
    matched = 0
    for r_t, r_fam in ref_events:
        best_j = None
        best_d = None
        for j in remaining:
            g_t, g_fam = gen_events[j]
            if use_family_match and g_fam != r_fam:
                continue
            d = abs(g_t - r_t)
            if best_d is None or d < best_d:
                best_d = d
                best_j = j
        if best_j is not None and best_d is not None and best_d <= tol_sec:
            matched += 1
            remaining.remove(best_j)
    if use_f1:
        precision = matched / len(gen_events) if gen_events else 0.0
        recall = matched / len(ref_events) if ref_events else 0.0
        f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
        return {"score": f1, "matched": matched, "precision": precision, "recall": recall,
                "ref_count": len(ref_events), "gen_count": len(gen_events), "tol_sec": tol_sec}
    denom = max(len(ref_events), 1)
    return {"score": matched / denom, "matched": matched,
            "ref_count": len(ref_events), "gen_count": len(gen_events), "tol_sec": tol_sec}


def _sfx_placement(spec: Dict[str, Any], plan: Dict[str, Any], tol_sec: float = 0.15) -> Dict[str, Any]:
    """R3: sfx_placement を「参考の顕著オンセット」ベースに変更。

    - **新スコア (score / sfx_placement)**: 参考の顕著オンセット（`select_salient_onsets`
      で抽出。plan 側の SE 枠と同数程度）に対して、plan の SE が ±0.3s 窓でカバーする
      F1（precision × recall の調和平均）。denom は「参考 = 生成 = 同じ ~target/2.5 個」
      で揃うため、参考 vs plan の分布一致を正しく測れる。family 一致は要求しない
      （分布一致が本命指標。family/timbre は render 段の別 QA で担保）。
    - **旧スコア (score_raw / sfx_placement_raw)**: 従来どおり「参考の全オンセット」を
      denom とした matched / |ref| （±0.15s + family 一致）。旧値を並記することで、
      指標変更の履歴と誠実性を担保する。
    """
    ref_all = _sfx_events_from_reference(spec)
    gen = _sfx_events_from_plan(plan)
    # 座標系を揃える（ref を生成尺スケール）
    ref_total = float((spec or {}).get("duration_sec") or 0.0)
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in (plan or {}).get("shots") or [])
    ratio = (gen_total / ref_total) if (ref_total > 0 and gen_total > 0) else 1.0
    ref_all_scaled = [(t * ratio, fam) for t, fam in ref_all]

    # 旧基準（後方互換の raw スコア）
    raw = _sfx_placement_score(ref_all_scaled, gen, tol_sec=float(tol_sec),
                                use_family_match=True, use_f1=False)

    # 新基準（顕著オンセット × ±0.3s × F1、family 一致は不要）
    salient = _salient_sfx_events_from_reference(spec, gen_total_sec=gen_total)
    salient_scaled = [(t * ratio, fam) for t, fam in salient]
    from pipeline.director import _SFX_SALIENT_MATCH_WINDOW_SEC
    new = _sfx_placement_score(salient_scaled, gen,
                                tol_sec=float(_SFX_SALIENT_MATCH_WINDOW_SEC),
                                use_family_match=False, use_f1=True)

    return {
        # 「score」は summary.sfx_placement に反映される（新基準）
        "score": new["score"],
        "score_raw": raw["score"],
        "matched": new["matched"],
        "ref_count": new["ref_count"],
        "gen_count": new["gen_count"],
        "tol_sec": new["tol_sec"],
        "precision": new.get("precision"),
        "recall": new.get("recall"),
        # 参考: 旧基準の詳細（比較用）
        "raw_detail": {
            "matched": raw["matched"],
            "ref_count": raw["ref_count"],
            "gen_count": raw["gen_count"],
            "tol_sec": raw["tol_sec"],
        },
    }


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
# 6. beat_alignment（R2a F5・shot境界の拍一致率）
# ---------------------------------------------------------------------------

def _beat_alignment(reference_spec: Dict[str, Any], plan: Dict[str, Any], tol_sec: float = 0.08) -> Dict[str, Any]:
    """生成 plan の shot 境界のうち、参考ビートグリッド (music.beat_times) の
    最寄り拍 ±tol_sec 以内に収まっているものの割合を返す。

    参考尺 -> 生成尺 に beat_times をスケールしてから比較する（座標系を揃える）。
    music が無い/beat_times が空/confidence が 0 の場合は unmeasured 扱い（score=None）
    を返し、summary 側では 0.0 ではなく None として扱う（後方互換）。
    """
    music = (reference_spec or {}).get("music") or {}
    beats = music.get("beat_times") or []
    conf = music.get("confidence")
    if not beats or (isinstance(conf, (int, float)) and float(conf) <= 0):
        return {"score": None, "matched": 0, "boundaries": 0, "beat_count": len(beats),
                "unmeasured_reason": "no_beats"}
    # 座標系揃え
    ref_total = float((reference_spec or {}).get("duration_sec") or 0.0)
    shots = (plan or {}).get("shots") or []
    gen_total = sum(float(s.get("duration_sec") or 0.0) for s in shots)
    if ref_total > 0 and gen_total > 0:
        ratio = gen_total / ref_total
        beats_scaled = sorted([float(t) * ratio for t in beats])
    else:
        beats_scaled = sorted(float(t) for t in beats)
    # 内部境界（先頭 0 と末尾は除外＝スナップ検証は「編集で選ばれた切り替え点」のみが対象）
    boundaries: List[float] = []
    cursor = 0.0
    for i, s in enumerate(shots):
        cursor += float(s.get("duration_sec") or 0.0)
        if i < len(shots) - 1:
            boundaries.append(cursor)
    if not boundaries:
        return {"score": None, "matched": 0, "boundaries": 0, "beat_count": len(beats),
                "unmeasured_reason": "no_boundaries"}
    matched = 0
    for b in boundaries:
        # 二分探索的に最寄り拍を求める
        lo, hi = 0, len(beats_scaled) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if beats_scaled[mid] < b:
                lo = mid + 1
            else:
                hi = mid
        cands = []
        if lo > 0:
            cands.append(abs(b - beats_scaled[lo - 1]))
        if lo < len(beats_scaled):
            cands.append(abs(b - beats_scaled[lo]))
        if cands and min(cands) <= tol_sec:
            matched += 1
    return {"score": matched / len(boundaries), "matched": matched,
            "boundaries": len(boundaries), "beat_count": len(beats),
            "tol_sec": tol_sec, "bpm": music.get("bpm"),
            "music_confidence": conf}


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
    # R3: 座標系を揃える。skeleton は「参考カット境界内の相対位置 × post-beat_snap の
    # ターゲットショット尺」で caption offset を組み立てる（piecewise）。fidelity 側も
    # 同じ piecewise 写像を使わないと、beat_snap で境界がずれた分だけ IoU が構造的に
    # 目減りする（例: 0.2秒ずれで IoU=0.84 に劣化）。cuts と plan.shots の件数が
    # 一致するとき（＝ skeleton から直で組んだ plan）は piecewise、それ以外は従来の
    # 線形一括スケール（後方互換）。
    ref_telops = _scale_ref_intervals_piecewise_or_linear(
        ref_telops, reference_spec, plan, ref_total, gen_total,
    )
    telop_iou = _telop_iou_avg(ref_telops, gen_telops)

    telop_style = _telop_style_match(reference_spec, plan)
    sfx = _sfx_placement(reference_spec, plan)
    cam = _camera_move_match(reference_spec, plan)
    beat = _beat_alignment(reference_spec, plan)

    summary = {
        "cut_match": cut_res["f1"],
        "telop_iou": telop_iou["iou_avg"],
        "telop_style": telop_style["score"],
        # R3: 新基準 = 参考の顕著オンセット × ±0.3s × F1（denom を plan と揃えて公正化）
        "sfx_placement": sfx["score"],
        # R3: 旧基準（参考全オンセット × ±0.15s × family一致）— 誠実性のため並記
        "sfx_placement_raw": sfx.get("score_raw"),
        "camera_move": cam["score"],
        "beat_alignment": beat.get("score"),  # None なら未測定
    }
    return {
        "summary": summary,
        "details": {
            "cut_match": cut_res,
            "telop_iou": telop_iou,
            "telop_style": telop_style,
            "sfx_placement": sfx,
            "camera_move": cam,
            "beat_alignment": beat,
        },
        "meta": {
            "reference_duration_sec": ref_total,
            "generated_duration_sec": gen_total,
            "reference_cuts_count": len(ref_cuts),
            "generated_shots_count": len((plan or {}).get("shots") or []),
        },
    }


# ---------------------------------------------------------------------------
# 7. product_subordination（P2・商品モードが cut/camera/beat 骨格を歪めていないか）
# ---------------------------------------------------------------------------

# 骨格系指標（この差分が ±SUBORDINATION_TOLERANCE 以内なら「商品は従属」と判定）:
# telop_iou / telop_style は telop の文言や配置に依るため対象外
# （商品情報を加えると telop 表現が変わりうるが、参考の cut/camera/beat 構造を
# 壊さなければ subordination_ok とみなす）。
_SUBORDINATION_SKELETON_METRICS = ("cut_match", "camera_move", "beat_alignment")


def compute_product_subordination(
    fid_no_product: Dict[str, Any],
    fid_with_product: Dict[str, Any],
    tolerance: float = 0.1,
) -> Dict[str, Any]:
    """商品ありplan / 商品なしplan の fidelity 差から、商品モードが参考の骨格
    （cut / camera / beat）を歪めていないかを判定する。

    Args:
        fid_no_product: compute_fidelity() の結果（商品なしplan）
        fid_with_product: 同（商品ありplan）
        tolerance: 骨格系指標の許容差。既定 ±0.1。

    Returns: {
        "subordination_ok": bool,
        "tolerance": float,
        "diffs": {metric: {"no_product": v1, "with_product": v2, "delta": abs(v1-v2), "within": bool}},
        "unmeasurable": [metric,...]  # どちらかが None（未測定）で比較不能な指標
    }
    """
    sum_no = (fid_no_product or {}).get("summary") or {}
    sum_with = (fid_with_product or {}).get("summary") or {}
    diffs: Dict[str, Any] = {}
    unmeasurable: List[str] = []
    ok = True
    for metric in _SUBORDINATION_SKELETON_METRICS:
        v1 = sum_no.get(metric)
        v2 = sum_with.get(metric)
        if v1 is None or v2 is None:
            unmeasurable.append(metric)
            continue
        try:
            delta = abs(float(v1) - float(v2))
        except (TypeError, ValueError):
            unmeasurable.append(metric)
            continue
        within = delta <= float(tolerance) + 1e-9
        diffs[metric] = {
            "no_product": float(v1),
            "with_product": float(v2),
            "delta": round(delta, 6),
            "within": bool(within),
        }
        if not within:
            ok = False
    return {
        "subordination_ok": bool(ok),
        "tolerance": float(tolerance),
        "diffs": diffs,
        "unmeasurable": unmeasurable,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TTP fidelity — 参考動画再現度の5指標を JSON で出力")
    p.add_argument("--reference-spec", required=True, help="reference_spec v2 の JSON パス")
    p.add_argument("--plan", required=True, help="生成 plan v2 の JSON パス")
    p.add_argument("--plan-no-product", default=None,
                   help="商品なしplan v2 の JSON パス。指定時、product_subordination も算出する。"
                   "--plan は商品ありplan として解釈される。")
    p.add_argument("--subordination-tolerance", type=float, default=0.1,
                   help="product_subordination の骨格指標差の許容値（既定 0.1）")
    p.add_argument("--out", default=None, help="結果 JSON の保存先（省略時は stdout）")
    args = p.parse_args(argv)

    with open(args.reference_spec, "r", encoding="utf-8") as f:
        ref = json.load(f)
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    result = compute_fidelity(ref, plan)
    if args.plan_no_product:
        with open(args.plan_no_product, "r", encoding="utf-8") as f:
            plan_no = json.load(f)
        fid_no = compute_fidelity(ref, plan_no)
        result["product_subordination"] = compute_product_subordination(
            fid_no, result, tolerance=float(args.subordination_tolerance),
        )
        # 参照用に商品なし側の summary も併記（差分の由来を追える）
        result["no_product_summary"] = fid_no["summary"]
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
