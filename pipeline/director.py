# -*- coding: utf-8 -*-
"""Fable 5「AIディレクター」呼び出し・矯正リトライ・スケルトン写像TTP企画生成。

TTP v2 移行後の設計:
  - 参考動画から抽出した reference_spec v2 (cuts/shots_ref/telops/sfx_events/
    hook_end_sec/cta_start_sec) をもとに、build_shot_skeleton() が「shots の骨・
    caption offset・sfx_plan・hook/cta shot_id」を機械的に組み立てる（純関数）。
  - LLM の役割は各 shot の narration_jp / caption_jp / visual_prompt(英語) の
    3フィールドを埋めることだけ。骨（構成）は既に決まっている。
  - LLM の出力は shot数一致・duration_sec 一致(±0.25s)・caption offset 一致・
    sfx_plan/hook_end_shot_id/cta_start_shot_id 不変を validate し、
    不合格なら矯正リトライ。全滅時は例外を投げる（スモークにフォールバックしない）。
  - `--reference-url` 無しでの LLM 企画生成は禁止（run_director は明示エラー）。
    no_llm=True のときのみ build_smoke_plan で最小plan を返す。

quality="supreme" では従来の3段生成のうち write→polish のみを流用する:
  1. write   : スケルトン注入プロンプトで台本(埋め字)を書かせる。
  2. polish  : critique プロンプトで書き直し（不合格ならドラフト採用）。
angles(切り口3案生成)は参考動画のビート構成が既に切り口を決めるためスキップする。
quality="single" は write のみの一発出し。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import copy
import json
import math
import os

try:
    from pipeline.claude_runner import call_claude_json
except Exception:  # pragma: no cover - claude CLI周りが未整備でもimportを壊さない
    call_claude_json = None

from pipeline import plan_schema

try:
    from pipeline.reference import find_verbatim_overlap
except Exception:  # pragma: no cover - reference.py周りが未整備でもimportを壊さない
    find_verbatim_overlap = None

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")

MAX_RETRIES = 2
POLISH_MAX_RETRIES = 1
DEFAULT_TARGET_TOLERANCE_SEC = 8.0
DEFAULT_QUALITY = "supreme"
# F12: supreme_plus = 品質最優先プリセット。write→critique→rewrite の3段（既存 supreme は
# write→polish の2段）。config.local/quality_max.json で有効化する想定。
QUALITY_LEVELS = ("supreme", "single", "supreme_plus")

# ---------------------------------------------------------------------------
# TTP スケルトン組み立てパラメータ
# ---------------------------------------------------------------------------
_SKELETON_MIN_SHOT_SEC = 1.2
_SKELETON_MAX_SFX_PER_SHOT = 2
_SKELETON_SFX_SEC_DIVISOR = 2.5  # 全体上限 = target_duration_sec / 2.5
_SKELETON_DURATION_MATCH_TOL = 0.25  # LLM出力の各duration一致許容
_SKELETON_SUM_MATCH_TOL = 0.1        # 合計尺の一致許容
_SKELETON_CAPTION_MATCH_TOL = 0.05   # caption offset一致許容

# sfx kind -> family マッピング
_SFX_KIND_TO_FAMILY = {
    "transition": "whoosh",
    "impact": "impact",
    "riser": "riser",
    "pop": "pop",
    "shimmer": "shimmer",
    # "other" は除外
}

# 15字以上の連続一致検出(丸写し検査)の閾値
_VERBATIM_OVERLAP_MIN_LEN = 15


class TTPReferenceRequiredError(RuntimeError):
    """LLM 経路で reference が未指定のときに送出する例外。

    TTP v2 移行後、参考動画無しでの LLM 企画生成は禁止（固定完成例からの丸暗記
    バグの温床になるため）。呼び出し側（CLI/Studio）で必ず `--reference-url` を
    渡すこと。
    """


class TTPSkeletonMismatchError(RuntimeError):
    """LLM 出力がスケルトンと不一致で、矯正リトライも全滅したときに送出する例外。"""


def _load_prompt(name):
    path = os.path.join(_PROMPT_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# スタイル名 -> プロンプトファイル名。未知のスタイル/未指定は既定(default)のプロンプトを使う。
_STYLE_PROMPT_FILES = {
    "default": "director_prompt.txt",
    "vertical_hook": "director_prompt_vertical_hook.txt",
}

_ANGLES_PROMPT_FILE = "angles_prompt.txt"
_CRITIQUE_PROMPT_FILE = "critique_prompt.txt"
_PRODUCT_BLOCK_PROMPT_FILE = "product_block.txt"
_REFERENCE_TTP_BLOCK_PROMPT_FILE = "reference_ttp_block.txt"

_VERTICAL_HOOK_STYLE_NOTE = "テロップは縦書き・高速カット(1カット約2秒)で見せるスタイル"


# ---------------------------------------------------------------------------
# TTP スケルトン組み立て
# ---------------------------------------------------------------------------

def _iter_boundaries_from_spec(reference_spec):
    """spec.cuts が優先。無ければ shots_ref から境界を取り出す。

    Returns: list[float]（区間の境界秒。0.0 を含み、末尾は duration_sec）
    """
    duration = float(reference_spec.get("duration_sec") or 0.0)
    cuts = reference_spec.get("cuts") or []
    if cuts:
        ts = []
        for c in cuts:
            if isinstance(c, dict) and isinstance(c.get("t"), (int, float)):
                ts.append(float(c["t"]))
            elif isinstance(c, (int, float)):
                ts.append(float(c))
        boundaries = [0.0] + sorted(t for t in ts if 0.0 < t < duration + 1e-3) + [duration]
    else:
        shots_ref = reference_spec.get("shots_ref") or []
        boundaries = [0.0]
        for s in shots_ref:
            end = s.get("end") if isinstance(s, dict) else None
            if isinstance(end, (int, float)) and end > boundaries[-1]:
                boundaries.append(float(end))
        if not boundaries or boundaries[-1] < duration - 1e-3:
            boundaries.append(duration)
    # 重複・単調性の掃除
    dedup = []
    for b in boundaries:
        if not dedup or b > dedup[-1] + 1e-3:
            dedup.append(round(b, 4))
    if len(dedup) < 2:
        dedup = [0.0, duration if duration > 0 else 1.0]
    return dedup


def _merge_boundaries_to_fit_min_shot(boundaries, target_duration_sec, min_shot_sec=_SKELETON_MIN_SHOT_SEC):
    """境界配列 boundaries から、shot数 <= floor(target_duration_sec / min_shot_sec) に収まるよう
    隣接カット(内部境界)をマージする。

    高速カット参考動画（cuts数 > target/min_shot）で min shot 尺(1.2s)保証と合計=target ±0.1s の
    両立ができず validate_plan が全滅する問題への対処。マージは「隣接 raw 尺の合計が最小の
    内部境界」を貪欲に落とす（短い連続カット同士を優先して統合）。

    telops / sfx_events は build_shot_skeleton 内で「参考尺座標の t」から
    _resolve_shot_id_at_time(boundaries, ...) で写像するため、境界を落とすだけで
    caption / sfx の対応 shot が自動的に統合先の shot へ付け替わる（別途の再マッピング不要）。

    len(boundaries) < 3（内部境界が無い=1shot）または max_shots <= 0 の場合は何もしない。
    """
    max_shots = max(1, int(float(target_duration_sec) / float(min_shot_sec)))
    b = list(boundaries)
    while len(b) - 1 > max_shots and len(b) >= 3:
        raws = [b[i + 1] - b[i] for i in range(len(b) - 1)]
        # 落とせるのは interior 境界 b[1..len-2]（両端 0.0 と duration は保持）
        best_i = 1
        best_sum = float("inf")
        for i in range(1, len(b) - 1):
            s = raws[i - 1] + raws[i]
            if s < best_sum:
                best_sum = s
                best_i = i
        del b[best_i]
    return b


def _scale_durations_to_target(raw_durations, target_duration_sec, min_shot_sec=_SKELETON_MIN_SHOT_SEC):
    """raw_durations を target_duration_sec に比例スケール。

    - 合計 = target_duration_sec ± 0.1s に収まるよう最終ショットで微調整。
    - 各 shot >= min_shot_sec を保証（不足分は最長 shot から借りる）。
    """
    total = sum(raw_durations) or 1.0
    scaled = [max(min_shot_sec, d * target_duration_sec / total) for d in raw_durations]

    # 各 shot >= min_shot_sec を強制した結果、合計が target を超過する場合は
    # 最大 shot から超過分を差し引く（差し引き後もその shot が min_shot_sec を
    # 下回らないようクランプ）。差し引き余地が足りない場合は諦める（下流の
    # target_tolerance で吸収）。
    for _ in range(50):
        s = sum(scaled)
        diff = s - target_duration_sec
        if abs(diff) < 0.01:
            break
        if diff > 0:
            # 超過: 最大 shot から削る
            i = max(range(len(scaled)), key=lambda k: scaled[k])
            take = min(diff, scaled[i] - min_shot_sec)
            if take <= 0:
                break
            scaled[i] -= take
        else:
            # 不足: 最大 shot に足す
            i = max(range(len(scaled)), key=lambda k: scaled[k])
            scaled[i] += -diff
    return [round(x, 3) for x in scaled]


def _resolve_shot_id_at_time(boundaries, shot_ids, t, prefer="right"):
    """boundaries[0..N] と shot_ids[0..N-1] から、時刻 t が属する shot_id を返す。

    prefer="right": t が境界にちょうど乗った場合は右側(次)の shot を返す
        （デフォルト。cta_start / telop start / sfx event 用）。
    prefer="left":  t が境界にちょうど乗った場合は左側(前)の shot を返す
        （hook_end 用。「フックが終わる shot」= 境界の1つ前）。
    """
    if t is None:
        return None
    tf = max(0.0, float(t))
    n = len(shot_ids)
    if prefer == "left":
        # 左寄せ: boundaries[i] < tf <= boundaries[i+1]
        for i in range(n):
            lo = boundaries[i]
            hi = boundaries[i + 1]
            if lo - 1e-6 < tf <= hi + 1e-6:
                return shot_ids[i]
        return shot_ids[-1]
    # 右寄せ(既定): boundaries[i] <= tf < boundaries[i+1]
    for i in range(n):
        lo = boundaries[i]
        hi = boundaries[i + 1]
        if lo - 1e-6 <= tf < hi - 1e-6:
            return shot_ids[i]
    # 末尾または末端超過は最後の shot に寄せる
    return shot_ids[-1]


def _map_reference_visual_to_shots(shots_ref, boundaries, shot_ids):
    """spec.shots_ref を境界 (参考尺座標) 上で shot_id に写像する（F10）。

    shots_ref は参考動画の実カット区間ごとに visual_desc_en / motion / (F3拡張で)
    shot_size / subject_count / camera_move / color_mood / framing / location /
    lighting / color_palette_hex 等を持つ dict。boundaries が境界マージで縮んで
    いる場合は「その shot 帯に完全に含まれる/重なる shots_ref を統合」して、
    先頭の visual_desc_en を採用しつつ motion/camera_move は多数決を取る。

    Returns: dict shot_id -> reference_visual dict
    """
    if not shots_ref or not shot_ids:
        return {}
    out = {}
    for i, sid in enumerate(shot_ids):
        lo = boundaries[i]
        hi = boundaries[i + 1]
        # 重なる shots_ref を全部拾う（半開区間: [lo, hi) と [ss, se) が真に重なるとき）。
        # 境界がぴったり一致（前 shot の終端 = 次 shot の開始）はどちらにも属さないため
        # 厳密不等号でリークを防ぐ（BUG-R1: 境界一致による reference_visual の隣接shotへの
        # 漏れを検出して修正）。
        overlaps = []
        for s in shots_ref:
            if not isinstance(s, dict):
                continue
            ss = s.get("start")
            se = s.get("end")
            if not isinstance(ss, (int, float)) or not isinstance(se, (int, float)):
                continue
            if float(ss) < hi - 1e-6 and float(se) > lo + 1e-6:
                overlaps.append(s)
        if not overlaps:
            continue
        # motion / camera_move / shot_size / color_mood は最頻値、それ以外は先頭の値
        def _mode(key, default=""):
            counts = {}
            for s in overlaps:
                v = s.get(key)
                if isinstance(v, str) and v:
                    counts[v] = counts.get(v, 0) + 1
            if not counts:
                return default
            return max(counts.items(), key=lambda kv: kv[1])[0]
        head = overlaps[0]
        rv = {
            "desc_en": (head.get("visual_desc_en") or "").strip(),
            "motion": _mode("motion", head.get("motion") or "static"),
            "shot_size": _mode("shot_size", ""),
            "subject_count": head.get("subject_count") if isinstance(head.get("subject_count"), int) else None,
            "camera_move": _mode("camera_move", head.get("motion") or ""),
            "color_mood": _mode("color_mood", ""),
            "framing": _mode("framing", ""),
            "location": _mode("location", ""),
            "lighting": _mode("lighting", ""),
            "color_palette_hex": head.get("color_palette_hex") or [],
        }
        # 空/None の値は落として skeleton_json を小さく保つ
        rv = {k: v for k, v in rv.items() if v not in (None, "", [], {})}
        if rv:
            out[sid] = rv
    return out


def _remap_reference_visual_by_split(rv_map, shot_ids, split_map, new_shot_ids):
    """reference_visual 写像を、旧 shot -> 分割群の**全メンバー**にコピーする（F10）。

    分割時、テロップは先頭のみに載せるが reference_visual は全断片が同じ絵から
    切り出したものなので分割群全員が同じ reference_visual を保持する。
    """
    if not rv_map:
        return {}
    remapped = {}
    for old_idx, old_sid in enumerate(shot_ids):
        rv = rv_map.get(old_sid)
        if not rv:
            continue
        new_group = split_map[old_idx] if old_idx < len(split_map) else [old_idx]
        for j in new_group:
            if 0 <= j < len(new_shot_ids):
                remapped[new_shot_ids[j]] = dict(rv)
    return remapped


def _map_telops_to_shots(telops, boundaries, shot_ids, scaled_durations, ref_duration):
    """spec.telops を対応 shot に写像し caption_in/out offset (相対秒) と style hint を返す。

    Returns: dict shot_id -> {"caption_in_offset_sec", "caption_out_offset_sec", "telop_style_hint"}
    """
    ratio = 0.0
    if ref_duration and ref_duration > 0:
        ratio = 1.0
    out = {}
    for tel in telops or []:
        if not isinstance(tel, dict):
            continue
        s = tel.get("start")
        e = tel.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            continue
        s = float(s)
        e = float(e)
        # 参考尺の s/e をそのまま参考尺の shot 帯へ落とす（境界は参考尺の実座標）。
        shot_id = _resolve_shot_id_at_time(boundaries, shot_ids, s)
        if shot_id is None:
            continue
        idx = shot_ids.index(shot_id)
        seg_start = boundaries[idx]
        seg_end = boundaries[idx + 1]
        seg_len = max(1e-3, seg_end - seg_start)
        # 参考区間内の相対比 → 目標尺の相対秒へ
        rel_in = max(0.0, (s - seg_start) / seg_len)
        rel_out = max(rel_in, (e - seg_start) / seg_len)
        rel_out = min(1.0, rel_out)
        target_dur = scaled_durations[idx]
        caption_in = round(rel_in * target_dur, 3)
        caption_out = round(rel_out * target_dur, 3)
        style = tel.get("style") or {}
        hint = {
            "position": tel.get("position") or "",
            "color": (style.get("color") or "") if isinstance(style, dict) else "",
            "stroke": (style.get("stroke") or "") if isinstance(style, dict) else "",
            "size_class": (style.get("size_class") or "") if isinstance(style, dict) else "",
            "emphasis_words": tel.get("emphasis_words") or [],
        }
        # 同一 shot に複数 telop が乗る場合は最初の一枚を採用（skeleton は 1shot=1telop 前提）。
        if shot_id not in out:
            out[shot_id] = {
                "caption_in_offset_sec": caption_in,
                "caption_out_offset_sec": caption_out,
                "telop_style_hint": hint,
            }
    return out


def _thin_sfx_events_for_shots(sfx_events, boundaries, shot_ids, scaled_durations, ref_duration, target_duration_sec, telops):
    """spec.sfx_events を confidence 降順で間引いて sfx_plan へ写像する。

    - 1 shot あたり最大 _SKELETON_MAX_SFX_PER_SHOT 発
    - 全体で target_duration_sec / _SKELETON_SFX_SEC_DIVISOR 発を上限
    - kind→family マッピング（"other" は除外）
    - anchor: 元の anchor が "cut" なら "cut"、
             telop の start ±0.3s に近い場合は "caption_in"、
             それ以外は "shot_start"
    """
    if not sfx_events or not shot_ids:
        return []
    global_limit = max(1, int(math.floor(target_duration_sec / _SKELETON_SFX_SEC_DIVISOR)))
    # confidence 降順に整列（同 confidence なら kind の優先順で: transition/impact/riser/pop/shimmer）
    kind_pri = {"transition": 0, "impact": 1, "riser": 2, "pop": 3, "shimmer": 4}
    events_sorted = sorted(
        [e for e in sfx_events if isinstance(e, dict)],
        key=lambda e: (-float(e.get("confidence") or 0.0), kind_pri.get(e.get("kind"), 9), float(e.get("t") or 0.0)),
    )

    # telop start ±0.3s の窓（参考尺座標）
    telop_starts = []
    for tel in telops or []:
        if isinstance(tel, dict) and isinstance(tel.get("start"), (int, float)):
            telop_starts.append(float(tel["start"]))

    per_shot_count = {sid: 0 for sid in shot_ids}
    result = []
    for ev in events_sorted:
        if len(result) >= global_limit:
            break
        kind = ev.get("kind")
        family = _SFX_KIND_TO_FAMILY.get(kind)
        if not family:
            continue
        t = ev.get("t")
        if not isinstance(t, (int, float)):
            continue
        tf = float(t)
        shot_id = _resolve_shot_id_at_time(boundaries, shot_ids, tf)
        if shot_id is None:
            continue
        if per_shot_count[shot_id] >= _SKELETON_MAX_SFX_PER_SHOT:
            continue
        idx = shot_ids.index(shot_id)
        seg_start = boundaries[idx]
        seg_end = boundaries[idx + 1]
        seg_len = max(1e-3, seg_end - seg_start)
        rel = max(0.0, min(1.0, (tf - seg_start) / seg_len))
        target_dur = scaled_durations[idx]
        offset_sec = round(rel * target_dur, 3)

        # anchor type 決定
        anchor_type = "shot_start"
        if ev.get("anchor") == "cut":
            anchor_type = "cut"
        else:
            # telop start ±0.3s(参考尺秒) に近ければ caption_in にする
            for ts in telop_starts:
                if abs(tf - ts) <= 0.3:
                    anchor_type = "caption_in"
                    break
        # duration_sec のクランプ
        offset_sec = max(0.0, min(offset_sec, target_dur))
        result.append({
            "t_anchor": {"type": anchor_type, "shot_id": shot_id, "offset_sec": offset_sec},
            "family": family,
        })
        per_shot_count[shot_id] += 1

    # t順に並べ替え
    def _sort_key(sp):
        anc = sp["t_anchor"]
        idx = shot_ids.index(anc["shot_id"])
        return (idx, anc["offset_sec"])
    result.sort(key=_sort_key)
    return result


def _split_long_shots(scaled_durations, max_shot_sec, min_shot_sec=_SKELETON_MIN_SHOT_SEC):
    """scaled_durations のうち max_shot_sec を超える shot を等分割する。

    Returns:
        (new_durations, split_map)
          new_durations: 分割後の各 shot 尺リスト
          split_map: 旧 index -> [新 index, ...] のリスト（1:1 なら [j] だけ、
                    1:m 分割なら [j, j+1, ...m-1個]）
    """
    if max_shot_sec is None or max_shot_sec <= 0:
        return list(scaled_durations), [[i] for i in range(len(scaled_durations))]
    new: list = []
    split_map: list = []
    for d in scaled_durations:
        d = float(d)
        if d <= float(max_shot_sec) + 1e-6:
            split_map.append([len(new)])
            new.append(d)
            continue
        # m = ceil(d / max_shot_sec)。ただし各断片が min_shot_sec 以上になるよう調整。
        m = int(math.ceil(d / float(max_shot_sec)))
        while m > 1 and (d / m) < float(min_shot_sec):
            m -= 1
        if m < 1:
            m = 1
        indices: list = []
        each = d / m
        for k in range(m):
            indices.append(len(new))
            new.append(round(each, 3))
        split_map.append(indices)
    return new, split_map


def _remap_telops_by_split(telop_map, shot_ids, split_map, new_shot_ids):
    """旧 shot_id -> {caption_in, caption_out, telop_style_hint} を分割後の shot に写像する。

    分割時、テロップは分割群の**先頭のみ**に付与し、offset は先頭 shot の尺に合わせて
    クランプする（caption_in/out が先頭 shot の duration_sec を超える場合は先頭 shot 内に
    収める。テロップが表示中に切り替わっても構成崩れは起きない）。
    """
    if not telop_map:
        return {}
    remapped: dict = {}
    for old_idx, old_sid in enumerate(shot_ids):
        if old_sid not in telop_map:
            continue
        info = telop_map[old_sid]
        new_group = split_map[old_idx] if old_idx < len(split_map) else [old_idx]
        if not new_group:
            continue
        first_new_idx = new_group[0]
        new_sid = new_shot_ids[first_new_idx]
        remapped[new_sid] = dict(info)
    return remapped


def _remap_sfx_by_split(sfx_plan, shot_ids, new_shot_ids, split_map, new_durations):
    """sfx_plan の t_anchor.shot_id / offset_sec を分割後の shot 群に写像する。

    分割時、offset_sec は「旧 shot 内での位置」なので、それが新 shot 群の
    どの断片に属するかを判定して、断片内相対 offset に付け替える。
    """
    if not sfx_plan:
        return []
    remapped: list = []
    old_id_to_idx = {sid: i for i, sid in enumerate(shot_ids)}
    for ev in sfx_plan:
        if not isinstance(ev, dict):
            continue
        anc = ev.get("t_anchor") or {}
        old_sid = anc.get("shot_id")
        if old_sid not in old_id_to_idx:
            continue
        old_idx = old_id_to_idx[old_sid]
        group = split_map[old_idx] if old_idx < len(split_map) else [old_idx]
        offset = float(anc.get("offset_sec") or 0.0)
        # 分割群内で offset が属する断片を判定
        cursor = 0.0
        target_new_idx = group[0]
        target_offset = offset
        for j, new_idx in enumerate(group):
            dur = new_durations[new_idx]
            if offset <= cursor + dur + 1e-6 or j == len(group) - 1:
                target_new_idx = new_idx
                target_offset = max(0.0, offset - cursor)
                target_offset = min(target_offset, dur)
                break
            cursor += dur
        new_ev = dict(ev)
        new_ev["t_anchor"] = dict(anc)
        new_ev["t_anchor"]["shot_id"] = new_shot_ids[target_new_idx]
        new_ev["t_anchor"]["offset_sec"] = round(target_offset, 3)
        remapped.append(new_ev)
    return remapped


def build_shot_skeleton(reference_spec, target_duration_sec, max_shot_sec=None, min_shot_sec=None):
    """reference_spec v2 から shot スケルトンを機械的に組み立てる純関数。

    Args:
        reference_spec: pipeline.reference_v2.analyze_reference_v2() が返す spec（dict）。
            cuts / shots_ref / telops / sfx_events / hook_end_sec / cta_start_sec /
            duration_sec を使う。
        target_duration_sec: 目標尺（秒）。
        max_shot_sec: 任意。指定すると、これを超える長尺 shot を等分割する。
            Higgsfield 480p は 1shot=1秒あたり約1クレジット消費のため、
            config.higgsfield.max_credits_per_shot 相当を渡すとクレジット上限を
            尊重した shot 分割になる。分割時は caption/sfx を写像維持する。
        min_shot_sec: 任意。最小ショット尺（秒）。None なら既定 _SKELETON_MIN_SHOT_SEC=1.2。
            F12: supreme_plus プリセットで 0.5 に落とすと参考の高速カットを保持できる。

    Returns:
        skeleton dict:
        {
          "shots": [
            {"id", "duration_sec", "caption_in_offset_sec", "caption_out_offset_sec",
             "telop_style_hint", "reference_visual"}, ...
          ],
          "sfx_plan": [...],
          "hook_end_shot_id": str|None,
          "cta_start_shot_id": str|None,
        }
    """
    if not isinstance(reference_spec, dict):
        raise ValueError("reference_spec は dict である必要があります")
    if not isinstance(target_duration_sec, (int, float)) or target_duration_sec <= 0:
        raise ValueError("target_duration_sec は正の数値である必要があります")

    effective_min_shot_sec = float(min_shot_sec) if isinstance(min_shot_sec, (int, float)) and min_shot_sec > 0 else _SKELETON_MIN_SHOT_SEC

    ref_duration = float(reference_spec.get("duration_sec") or 0.0)
    boundaries = _iter_boundaries_from_spec(reference_spec)  # 参考尺座標
    # 高速カット参考動画（cuts数 > target/min_shot）で min shot 尺と target 合計が両立せず
    # validate_plan で全滅する問題への対処: 境界を貪欲マージして shot 数を減らす。
    boundaries = _merge_boundaries_to_fit_min_shot(
        boundaries, float(target_duration_sec), min_shot_sec=effective_min_shot_sec,
    )
    n = len(boundaries) - 1
    if n < 1:
        raise ValueError("reference_spec からショット区間が抽出できませんでした")

    raw_durations = [max(1e-3, boundaries[i + 1] - boundaries[i]) for i in range(n)]
    scaled = _scale_durations_to_target(
        raw_durations, float(target_duration_sec), min_shot_sec=effective_min_shot_sec,
    )
    shot_ids = ["s{}".format(i + 1) for i in range(n)]

    # telop 写像
    telop_map = _map_telops_to_shots(
        reference_spec.get("telops") or [],
        boundaries,
        shot_ids,
        scaled,
        ref_duration,
    )

    # F10: 参考映像情報の写像（shots_ref -> shot_id）。境界がマージされているため
    # 統合先の shot に「その帯に重なる」shots_ref を集約する。
    rv_map = _map_reference_visual_to_shots(
        reference_spec.get("shots_ref") or [],
        boundaries,
        shot_ids,
    )

    # sfx 間引き（写像は分割前の shot_ids でまず行う）
    sfx_plan = _thin_sfx_events_for_shots(
        reference_spec.get("sfx_events") or [],
        boundaries,
        shot_ids,
        scaled,
        ref_duration,
        float(target_duration_sec),
        reference_spec.get("telops") or [],
    )

    # hook/cta shot_id 解決（分割前の shot_ids で判定）
    hook_end_shot_id = None
    cta_start_shot_id = None
    hook_end_sec = reference_spec.get("hook_end_sec")
    cta_start_sec = reference_spec.get("cta_start_sec")
    if isinstance(hook_end_sec, (int, float)):
        hook_end_shot_id = _resolve_shot_id_at_time(
            boundaries, shot_ids, float(hook_end_sec), prefer="left"
        )
    if isinstance(cta_start_sec, (int, float)):
        cta_start_shot_id = _resolve_shot_id_at_time(
            boundaries, shot_ids, float(cta_start_sec), prefer="right"
        )

    # クレジット意識分割（max_shot_sec が指定されたとき）
    if max_shot_sec is not None and max_shot_sec > 0:
        new_durations, split_map = _split_long_shots(scaled, float(max_shot_sec), min_shot_sec=effective_min_shot_sec)
        new_shot_ids = ["s{}".format(i + 1) for i in range(len(new_durations))]
        # telop 写像を分割群の先頭 shot に載せ替え
        telop_map = _remap_telops_by_split(telop_map, shot_ids, split_map, new_shot_ids)
        # reference_visual 写像を分割群の全員にコピー（同じ絵の一部を切り出したもの）
        rv_map = _remap_reference_visual_by_split(rv_map, shot_ids, split_map, new_shot_ids)
        # sfx 写像を分割断片に載せ替え
        sfx_plan = _remap_sfx_by_split(sfx_plan, shot_ids, new_shot_ids, split_map, new_durations)
        # hook/cta shot_id の載せ替え（旧 -> 分割群の先頭）
        old_id_to_idx = {sid: i for i, sid in enumerate(shot_ids)}
        if hook_end_shot_id in old_id_to_idx:
            g = split_map[old_id_to_idx[hook_end_shot_id]]
            hook_end_shot_id = new_shot_ids[g[-1]] if g else hook_end_shot_id
        if cta_start_shot_id in old_id_to_idx:
            g = split_map[old_id_to_idx[cta_start_shot_id]]
            cta_start_shot_id = new_shot_ids[g[0]] if g else cta_start_shot_id
        scaled = new_durations
        shot_ids = new_shot_ids

    shots = []
    for i, sid in enumerate(shot_ids):
        shot = {
            "id": sid,
            "duration_sec": float(scaled[i]),
        }
        if sid in telop_map:
            info = telop_map[sid]
            # caption offset は shot 尺内へクランプ
            cin = float(info.get("caption_in_offset_sec") or 0.0)
            cout = float(info.get("caption_out_offset_sec") or 0.0)
            cin = max(0.0, min(cin, float(scaled[i])))
            cout = max(cin, min(cout, float(scaled[i])))
            shot["caption_in_offset_sec"] = round(cin, 3)
            shot["caption_out_offset_sec"] = round(cout, 3)
            shot["telop_style_hint"] = info.get("telop_style_hint")
        # F10: 参考映像情報を skeleton の各 shot に載せる
        if sid in rv_map:
            shot["reference_visual"] = rv_map[sid]
        shots.append(shot)

    return {
        "shots": shots,
        "sfx_plan": sfx_plan,
        "hook_end_shot_id": hook_end_shot_id,
        "cta_start_shot_id": cta_start_shot_id,
    }


# ---------------------------------------------------------------------------
# 参考動画TTPブロック（skeleton 注入テンプレ）
# ---------------------------------------------------------------------------

def _build_reference_block(skeleton, target_duration_sec):
    """skeleton(dict) から {REFERENCE_TTP_BLOCK} 注入用テキストを組み立てる。

    skeleton=None（または空dict）の場合は空文字を返す。
    """
    if not skeleton:
        return ""
    template = _load_prompt(_REFERENCE_TTP_BLOCK_PROMPT_FILE)
    skeleton_json = json.dumps(skeleton, ensure_ascii=False, indent=2)
    body = (
        template.replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{SKELETON_JSON}", skeleton_json)
    )
    # 呼び出し側テンプレート({ANGLE_BLOCK}{REFERENCE_TTP_BLOCK}) の直前と改行境界が無いため
    # 先頭に改行を1つ入れる（reference未指定時は空文字を返すので既存出力は不変）。
    return "\n" + body


def _build_product_block(product):
    if not product:
        return ""
    template = _load_prompt(_PRODUCT_BLOCK_PROMPT_FILE)
    name = (product.get("name") or "この商品").strip()
    image_count = product.get("image_count")
    try:
        image_count = int(image_count)
    except (TypeError, ValueError):
        image_count = 0
    return template.replace("{PRODUCT_NAME}", name).replace("{IMAGE_COUNT}", str(image_count))


def build_director_prompt(theme, target_duration_sec, target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC,
                           style="default", angle_block="", product=None, reference_block=""):
    template = _load_prompt(_STYLE_PROMPT_FILES.get(style, _STYLE_PROMPT_FILES["default"]))
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{ANGLE_BLOCK}", angle_block or "")
        .replace("{PRODUCT_BLOCK}", _build_product_block(product))
        .replace("{REFERENCE_TTP_BLOCK}", reference_block or "")
    )


def build_corrective_prompt(base_prompt, errors):
    header = "前回の出力は以下のエラーがあり不合格でした。エラーを全て解消し、JSONのみを再出力してください。\n"
    header += "\n".join("- {}".format(e) for e in errors)
    header += "\n\n---\n\n"
    return header + base_prompt


# ---------------------------------------------------------------------------
# スケルトンとの一致検査（LLM出力の骨崩れを検出）
# ---------------------------------------------------------------------------

def _validate_plan_matches_skeleton(plan, skeleton, target_duration_sec):
    """plan が skeleton の骨（shot数・duration・caption offset・sfx_plan・hook/cta id）
    を保持しているか検査する。ズレていればエラー文字列のリストを返す（0件なら合格）。
    """
    errors = []
    plan_shots = plan.get("shots") or []
    skel_shots = skeleton.get("shots") or []
    if len(plan_shots) != len(skel_shots):
        errors.append(
            "shots数がスケルトンと一致しません(plan={}, skeleton={}). スケルトンの構成を保持し、"
            "shots要素の増減はせず埋め字のみを行ってください。".format(len(plan_shots), len(skel_shots))
        )
        return errors

    for i, (ps, ss) in enumerate(zip(plan_shots, skel_shots)):
        pid = ps.get("id")
        sid = ss.get("id")
        if pid != sid:
            errors.append(
                "shots[{}].id がスケルトンと一致しません(plan={!r}, skeleton={!r}). "
                "id を変更しないでください。".format(i, pid, sid)
            )
        pd = ps.get("duration_sec")
        sd = ss.get("duration_sec")
        if not isinstance(pd, (int, float)) or abs(float(pd) - float(sd)) > _SKELETON_DURATION_MATCH_TOL:
            errors.append(
                "shots[{}(id={})].duration_sec がスケルトンと一致しません(plan={}, skeleton={}, 許容±{}秒). "
                "スケルトンの秒数をそのまま返してください。".format(
                    i, sid, pd, sd, _SKELETON_DURATION_MATCH_TOL
                )
            )
        # caption offset の一致（スケルトンに存在するときのみ）
        for key in ("caption_in_offset_sec", "caption_out_offset_sec"):
            if key in ss:
                pv = ps.get(key)
                sv = ss.get(key)
                if not isinstance(pv, (int, float)) or abs(float(pv) - float(sv)) > _SKELETON_CAPTION_MATCH_TOL:
                    errors.append(
                        "shots[{}(id={})].{} がスケルトンと一致しません(plan={}, skeleton={}, 許容±{}秒). "
                        "スケルトン値をそのまま返してください。".format(i, sid, key, pv, sv, _SKELETON_CAPTION_MATCH_TOL)
                    )

    # 合計尺の一致
    total = sum(float(s.get("duration_sec") or 0.0) for s in plan_shots)
    skel_total = sum(float(s.get("duration_sec") or 0.0) for s in skel_shots)
    if abs(total - skel_total) > _SKELETON_SUM_MATCH_TOL:
        errors.append(
            "shots.duration_sec 合計がスケルトンと一致しません(plan={:.3f}, skeleton={:.3f}, 許容±{}秒)".format(
                total, skel_total, _SKELETON_SUM_MATCH_TOL
            )
        )

    # sfx_plan の完全一致
    plan_sfx = plan.get("sfx_plan")
    skel_sfx = skeleton.get("sfx_plan") or []
    if plan_sfx is None:
        if skel_sfx:
            errors.append(
                "sfx_plan がスケルトンと不一致(plan=None, skeleton件数={}). スケルトンの sfx_plan を"
                "そのまま返してください。".format(len(skel_sfx))
            )
    else:
        if not isinstance(plan_sfx, list) or len(plan_sfx) != len(skel_sfx):
            errors.append(
                "sfx_plan 件数がスケルトンと不一致(plan={}, skeleton={}). スケルトンの sfx_plan を"
                "そのまま返してください。".format(
                    len(plan_sfx) if isinstance(plan_sfx, list) else "非リスト", len(skel_sfx)
                )
            )
        else:
            for i, (pe, se) in enumerate(zip(plan_sfx, skel_sfx)):
                if not isinstance(pe, dict):
                    errors.append("sfx_plan[{}] はオブジェクトである必要があります".format(i))
                    continue
                pa = pe.get("t_anchor") or {}
                sa = se.get("t_anchor") or {}
                if pa.get("type") != sa.get("type") or pa.get("shot_id") != sa.get("shot_id"):
                    errors.append(
                        "sfx_plan[{}].t_anchor がスケルトンと不一致 (plan={}, skeleton={})".format(
                            i, {"type": pa.get("type"), "shot_id": pa.get("shot_id")},
                            {"type": sa.get("type"), "shot_id": sa.get("shot_id")}
                        )
                    )
                if not isinstance(pa.get("offset_sec"), (int, float)) or \
                        abs(float(pa.get("offset_sec")) - float(sa.get("offset_sec"))) > _SKELETON_CAPTION_MATCH_TOL:
                    errors.append(
                        "sfx_plan[{}].t_anchor.offset_sec がスケルトンと不一致 (plan={}, skeleton={}, 許容±{}秒)".format(
                            i, pa.get("offset_sec"), sa.get("offset_sec"), _SKELETON_CAPTION_MATCH_TOL
                        )
                    )
                if pe.get("family") != se.get("family"):
                    errors.append(
                        "sfx_plan[{}].family がスケルトンと不一致 (plan={!r}, skeleton={!r})".format(
                            i, pe.get("family"), se.get("family")
                        )
                    )

    # hook_end_shot_id / cta_start_shot_id
    for key in ("hook_end_shot_id", "cta_start_shot_id"):
        if skeleton.get(key) is not None and plan.get(key) != skeleton.get(key):
            errors.append(
                "{} がスケルトンと不一致(plan={!r}, skeleton={!r}). スケルトン値をそのまま返してください。".format(
                    key, plan.get(key), skeleton.get(key)
                )
            )

    return errors


def _find_verbatim_overlap_in_texts(reference_texts, script_texts, min_len=_VERBATIM_OVERLAP_MIN_LEN):
    """複数の参考テキスト(transcript/telop文言)と複数の script テキストの間で
    15字以上連続一致する箇所を検出する。"""
    if find_verbatim_overlap is None:
        return []
    overlaps = []
    for ref in reference_texts:
        if not ref:
            continue
        for s in script_texts:
            if not s:
                continue
            for chunk in find_verbatim_overlap(ref, s, min_len=min_len) or []:
                overlaps.append(chunk)
    # 重複除去
    seen = set()
    out = []
    for c in overlaps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Stage2: write（スケルトン注入付き。検証+矯正リトライ）
# ---------------------------------------------------------------------------

def _attempt_plan(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                    trace=None, skeleton=None, reference=None):
    """claude呼び出し→バリデーション→不合格なら矯正プロンプトで再帰リトライ。全滅時は None。

    skeleton を渡すと、plan_schema.validate_plan の後に骨一致検査 (
    _validate_plan_matches_skeleton) を追加で行う（LLM の骨崩れを検出）。
    reference を渡すと、丸写し検査(15字連続一致)を narration_script と各 shot の
    caption_jp / narration_jp に対して実施する。
    """
    if call_claude_json is None:
        return None
    timeout_sec = (config or {}).get("claude_timeout_sec", 600)
    try:
        result = call_claude_json(prompt, timeout_sec=timeout_sec)
    except Exception as exc:
        result = {"ok": False, "data": None, "error": str(exc)}

    if trace is not None:
        trace["last_model_used"] = (result or {}).get("model_used")

    if not result or not result.get("ok") or not isinstance(result.get("data"), dict):
        if retries_left <= 0:
            return None
        err = (result or {}).get("error") or "応答が取得できませんでした"
        corrective = build_corrective_prompt(prompt, [err])
        return _attempt_plan(
            corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec,
            trace=trace, skeleton=skeleton, reference=reference,
        )

    candidate = dict(result["data"])
    meta = dict(candidate.get("meta") or {})
    meta["model_used"] = result.get("model_used")
    meta["fallback_from"] = result.get("fallback_from")
    meta["fallback_reason"] = result.get("fallback_reason")
    meta.setdefault("source", "ai")
    candidate["meta"] = meta

    ok, errors, normalized = plan_schema.validate_plan(
        candidate, target_duration_sec=target_duration_sec, target_tolerance_sec=target_tolerance_sec
    )
    if ok and skeleton is not None:
        # 骨一致検査
        skel_errors = _validate_plan_matches_skeleton(normalized, skeleton, target_duration_sec)
        if skel_errors:
            ok = False
            errors = skel_errors
    if ok and reference is not None:
        # 丸写し検査(15字以上連続一致)
        ref_texts = []
        transcript = reference.get("transcript") if isinstance(reference, dict) else None
        if transcript:
            ref_texts.append(transcript)
        for tel in (reference.get("telops") or []) if isinstance(reference, dict) else []:
            if isinstance(tel, dict) and isinstance(tel.get("text"), str):
                ref_texts.append(tel["text"])
        script_texts = [normalized.get("narration_script") or ""]
        for s in normalized.get("shots") or []:
            script_texts.append(s.get("caption_jp") or "")
            script_texts.append(s.get("narration_jp") or "")
        overlaps = _find_verbatim_overlap_in_texts(ref_texts, script_texts)
        if overlaps:
            ok = False
            errors = [
                "参考動画の文言と{}字以上連続一致する丸写し箇所があります(narration_script/caption_jp/narration_jp)"
                "。表現は必ず自分の言葉に言い換えてください。検出例: {}".format(
                    _VERBATIM_OVERLAP_MIN_LEN, overlaps[:3]
                )
            ]

    if ok:
        return normalized
    if retries_left <= 0:
        return None
    corrective = build_corrective_prompt(prompt, errors)
    return _attempt_plan(
        corrective, config, retries_left - 1, target_duration_sec, target_tolerance_sec,
        trace=trace, skeleton=skeleton, reference=reference,
    )


# ---------------------------------------------------------------------------
# Stage3: polish
# ---------------------------------------------------------------------------

def build_critique_prompt(theme, target_duration_sec, target_tolerance_sec, style, draft_plan, reference_block=""):
    template = _load_prompt(_CRITIQUE_PROMPT_FILE)
    draft_json = json.dumps(draft_plan, ensure_ascii=False, indent=2)
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{TARGET_TOLERANCE}", "{:.0f}".format(target_tolerance_sec))
        .replace("{STYLE}", style)
        .replace("{DRAFT_JSON}", draft_json)
        .replace("{REFERENCE_TTP_BLOCK}", reference_block or "")
    )


# ---------------------------------------------------------------------------
# run_director（TTP モードへ全面移行）
# ---------------------------------------------------------------------------

def run_director(theme, config=None, target_duration_sec=None, no_llm=False,
                  target_tolerance_sec=DEFAULT_TARGET_TOLERANCE_SEC, style="default", quality=None,
                  product=None, reference=None):
    """テーマ + 参考動画spec v2 から reel_plan を生成する。

    no_llm=True: claude 呼び出しを一切行わず、build_smoke_plan で最小plan を返す
    （テスト / スモーク用。本番企画には使わない）。
    reference: pipeline.reference_v2.analyze_reference_v2() が返す spec v2（dict）。
        指定必須（no_llm=False かつ call_claude_json が有効なとき）。無い場合は
        TTPReferenceRequiredError を送出する（--reference-url 必須の案内文つき）。
    quality: "supreme"（既定・write→polish の2段） / "single"（write のみ）。
    style: "default" / "vertical_hook"（プロンプトの書き分けのみ）。
    product: 商品アフィリエイト動画モード用の商品情報 {"name","url","image_count"}。

    Raises:
        TTPReferenceRequiredError: reference=None かつ no_llm=False のとき。
        TTPSkeletonMismatchError: LLM 出力がスケルトンと一致せず矯正リトライも全滅したとき。
    """
    config = config or {}
    if target_duration_sec is None:
        target_duration_sec = config.get("target_duration_sec", 30)
    if quality is None:
        quality = config.get("director_quality", DEFAULT_QUALITY)
    if quality not in QUALITY_LEVELS:
        quality = DEFAULT_QUALITY

    if no_llm:
        # 後方互換のため build_rule_based_plan (= build_smoke_plan の薄い alias) を使う。
        # source="rule" で返る（旧テストとの整合。中身は smoke plan と同じ）。
        plan = plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec, style=style)
        plan.setdefault("meta", {})
        plan["meta"]["style"] = style
        plan["meta"]["quality"] = quality
        plan["meta"]["product"] = (product or {}).get("name") if product else None
        return plan

    # TTP v2 移行: LLM 経路では reference が必須。
    if reference is None:
        raise TTPReferenceRequiredError(
            "参考動画URLが指定されていません。TTP v2 移行後、LLM 経路の企画生成は"
            "reference（--reference-url で解析された reference_spec v2）が必須です。"
            "run.py / Studio 側から --reference-url を渡すか、テストでは "
            "no_llm=True を指定してスモーク plan を使ってください。"
        )
    if call_claude_json is None:
        # call_claude_json が None（claude_runner 未整備）の場合も、TTP モードの LLM
        # 呼び出しは成立しないため例外を投げる。テストでこの経路を叩きたい場合は
        # no_llm=True を使うこと。
        raise TTPReferenceRequiredError(
            "claude_runner.call_claude_json が利用できないため LLM 経路の企画生成は"
            "実行不能です。テスト用途では no_llm=True を指定してスモーク plan を"
            "使ってください。"
        )

    # スケルトン組み立て（純関数）
    # クレジット意識分割: config.higgsfield.max_credits_per_shot を 480p の
    # credits≒秒 換算で shot 最大尺として渡す（config が無ければ None＝分割なし）。
    hf_cfg = (config or {}).get("higgsfield") or {}
    max_shot_sec = None
    max_credits = hf_cfg.get("max_credits_per_shot")
    if isinstance(max_credits, (int, float)) and max_credits > 0:
        max_shot_sec = float(max_credits)
    # F12: config.director.min_shot_sec があれば skeleton の最小ショット尺として渡す
    # （supreme_plus プリセットで 0.5 に落として参考の高速カットを保持する）。
    director_cfg = (config or {}).get("director") or {}
    min_shot_sec_cfg = director_cfg.get("min_shot_sec")
    skeleton = build_shot_skeleton(
        reference, target_duration_sec, max_shot_sec=max_shot_sec,
        min_shot_sec=min_shot_sec_cfg if isinstance(min_shot_sec_cfg, (int, float)) and min_shot_sec_cfg > 0 else None,
    )
    # F12: 品質最優先の指示文（config.director.quality_directive）をプロンプトに注入する。
    # 未指定なら空文字。ここでは reference_block の末尾に挟むだけで、director_prompt.txt の
    # ロジックは触らない（後方互換）。
    quality_directive = director_cfg.get("quality_directive") if isinstance(director_cfg.get("quality_directive"), str) else ""
    reference_block = _build_reference_block(skeleton, target_duration_sec)
    if quality_directive:
        reference_block = reference_block + "\n\n# 品質最優先の追加指示（F12: supreme_plus）\n" + quality_directive + "\n"

    stages = {}
    # TTP モードでは angles(切り口3案生成)はスキップ（切り口=参考動画の構成に固定）。
    if quality in ("supreme", "supreme_plus"):
        stages["angles"] = {"ok": False, "skipped": "reference"}

    write_trace = {}
    prompt = build_director_prompt(
        theme, target_duration_sec, target_tolerance_sec, style=style, angle_block="", product=product,
        reference_block=reference_block,
    )
    plan = _attempt_plan(
        prompt, config, MAX_RETRIES, target_duration_sec, target_tolerance_sec,
        trace=write_trace, skeleton=skeleton, reference=reference,
    )
    stages["write"] = {"ok": plan is not None, "model_used": write_trace.get("last_model_used")}

    if plan is None:
        # 全滅（write 失敗）: スモークにフォールバックせず、例外を投げる。
        raise TTPSkeletonMismatchError(
            "TTP write 段が全滅しました（LLM がスケルトンの骨を守れない、または応答が取得"
            "できない）。参考動画URLを見直すか、target_duration_sec / スケルトン設定を"
            "調整してください。"
        )

    if quality in ("supreme", "supreme_plus"):
        polish_trace = {}
        critique_prompt = build_critique_prompt(
            theme, target_duration_sec, target_tolerance_sec, style, plan, reference_block=reference_block
        )
        polished = _attempt_plan(
            critique_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec,
            trace=polish_trace, skeleton=skeleton, reference=reference,
        )
        stages["polish"] = {"ok": polished is not None, "model_used": polish_trace.get("last_model_used")}
        if polished is not None:
            plan = polished
        # polish 不合格: write のドラフトをそのまま採用。

    if quality == "supreme_plus":
        # F12: 3段目 rewrite ステージ。critique 出力をもう一度 director プロンプトに戻し、
        # スケルトン厳守+参考映像情報の忠実反映を再確認させる。矯正リトライを1回だけ許す。
        rewrite_trace = {}
        rewrite_prompt = build_director_prompt(
            theme, target_duration_sec, target_tolerance_sec, style=style, angle_block="", product=product,
            reference_block=reference_block,
        )
        rewrite_prompt = (
            "以下のドラフトplanを『骨（構造）はそのまま保持し、visual_prompt/caption_jp/"
            "narration_jp の表現と参考映像情報の反映度だけをさらに向上させて再出力』してください。\n\n"
            "ドラフト:\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n\n---\n\n" + rewrite_prompt
        )
        rewritten = _attempt_plan(
            rewrite_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec,
            trace=rewrite_trace, skeleton=skeleton, reference=reference,
        )
        stages["rewrite"] = {"ok": rewritten is not None, "model_used": rewrite_trace.get("last_model_used")}
        if rewritten is not None:
            plan = rewritten

    plan.setdefault("meta", {})
    plan["meta"]["style"] = style
    plan["meta"]["quality"] = quality
    plan["meta"]["product"] = (product or {}).get("name") if product else None
    if quality in ("supreme", "supreme_plus"):
        plan["meta"]["stages"] = stages
    # 参考spec の要点を meta に記録（下流の render/検証で使える）
    plan["meta"]["reference_skeleton_shot_count"] = len(skeleton["shots"])
    plan["meta"]["reference_skeleton_sfx_count"] = len(skeleton.get("sfx_plan") or [])
    # クレジット意識分割の provenance（QA が同じスケルトンを再現できるように）
    plan["meta"]["skeleton_max_shot_sec"] = max_shot_sec
    plan["meta"]["target_duration_sec"] = float(target_duration_sec)
    return plan


# ---------------------------------------------------------------------------
# 後方互換のためのモジュール属性
# ---------------------------------------------------------------------------
# 旧テスト/コードが director.run_angles_stage を参照する可能性に備えて残す(no-op)。
# TTP モードでは angles は常にスキップされる。

def run_angles_stage(theme, config, target_duration_sec, style="default"):
    """後方互換のダミー実装。TTP v2 移行後、angles は常にスキップされる。"""
    return "", {"ok": False, "model_used": None, "skipped": "reference_ttp_v2_mode"}


def build_angles_prompt(theme, target_duration_sec, style="default"):
    """後方互換のためのプロンプト組み立て（実際には呼ばれない）。"""
    template = _load_prompt(_ANGLES_PROMPT_FILE)
    style_note = _VERTICAL_HOOK_STYLE_NOTE if style == "vertical_hook" else ""
    return (
        template.replace("{THEME}", theme)
        .replace("{TARGET_DURATION}", "{:.0f}".format(target_duration_sec))
        .replace("{STYLE_NOTE}", style_note)
    )
