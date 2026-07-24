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
import time
from typing import Any, Dict, List, Optional  # noqa: F401  R2a F5 type hints

try:
    from pipeline.claude_runner import call_claude_json
except Exception:  # pragma: no cover - claude CLI周りが未整備でもimportを壊さない
    call_claude_json = None

from pipeline import plan_schema

try:
    from pipeline.reference import find_verbatim_overlap
except Exception:  # pragma: no cover - reference.py周りが未整備でもimportを壊さない
    find_verbatim_overlap = None

try:
    from pipeline.music import snap_boundaries_to_beats
except Exception:  # pragma: no cover
    snap_boundaries_to_beats = None  # type: ignore

try:
    from pipeline.optical_flow import camera_move_to_preset
except Exception:  # pragma: no cover
    camera_move_to_preset = None  # type: ignore

try:
    from pipeline.narration_rhythm import expected_chars_for_shot
except Exception:  # pragma: no cover
    expected_chars_for_shot = None  # type: ignore

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

# R3: 顕著オンセット抽出用の kind 重み（transition/impact/riser を pop/shimmer より優先）。
# 参考動画の「重要な音」だけを選ぶ基準として、confidence * SALIENT_KIND_WEIGHT を用いる。
_SALIENT_KIND_WEIGHT = {
    "transition": 1.4,
    "impact": 1.2,
    "riser": 1.1,
    "pop": 0.7,
    "shimmer": 0.5,
}

# R3: 近接オンセットのクラスタ統合ギャップ（秒）。この間隔内に連続するオンセットは
# 1クラスタとみなし、代表1個（最大重み）に統合する。
_SALIENT_CLUSTER_GAP_SEC = 0.4

# R3: sfx_plan と参考オンセットの分布一致判定に使う目標時刻の許容差（秒）。
# アンカー（cut/caption_in）候補との差がこれ以下なら該当アンカーを採用する。
_SFX_ANCHOR_SNAP_SEC = 0.15

# R3: 参考顕著オンセット±ANCHOR_MATCH_WINDOW_SEC を「一致」とみなす窓（qa/fidelity 側）
_SFX_SALIENT_MATCH_WINDOW_SEC = 0.3

# 15字以上の連続一致検出(丸写し検査)の閾値
_VERBATIM_OVERLAP_MIN_LEN = 15

# F13: narration_jp 文字数バジェット。参考動画のリズム(=カット割り)を再現するため、
# 各 shot の narration_jp 文字数を「expected_narration_chars（無ければ 尺 × chars_per_sec、
# 既定 6.5字/秒）× (1 + 15%)」で拘束する。超過は矯正リトライ対象（TTS 実尺が伸びて
# 音声主導同期モードで shot が引き伸ばされる→ telop_iou / cut_match が崩れるのを防ぐ）。
_NARRATION_CHAR_BUDGET_TOLERANCE_RATIO = 0.15
_NARRATION_DEFAULT_CHARS_PER_SEC = 6.5


def _narration_char_budget(shot_dict, chars_per_sec_fallback):
    """1 shot ぶんの許容文字数（上限）を返す。0 以下なら None（検査対象外）を返す。

    優先順位:
      1. shot["expected_narration_chars"] があればそれを budget として使う。
      2. なければ shot["duration_sec"] × chars_per_sec_fallback（>0 のとき）。
      3. どちらも取れなければ None を返す（検査スキップ）。
    """
    budget = None
    ec = shot_dict.get("expected_narration_chars")
    if isinstance(ec, (int, float)) and ec > 0:
        budget = float(ec)
    else:
        dur = shot_dict.get("duration_sec")
        cps = chars_per_sec_fallback
        if isinstance(dur, (int, float)) and dur > 0 and isinstance(cps, (int, float)) and cps > 0:
            budget = float(dur) * float(cps)
    if budget is None or budget <= 0:
        return None
    max_chars = int(math.floor(budget * (1.0 + _NARRATION_CHAR_BUDGET_TOLERANCE_RATIO)))
    return max(1, max_chars)


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
            # R2b F2/F4: 光学フロー由来の強度（weak/strong）を skeleton まで運ぶ
            "intensity": _mode("intensity", ""),
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


# R4: 1 shot あたりに保持するテロップの最大件数（超過分は confidence 降順で切る）。
_MAX_TELOPS_PER_SHOT = 3


def _map_telops_to_shots(telops, boundaries, shot_ids, scaled_durations, ref_duration):
    """spec.telops を対応 shot に写像し、shot ごとの caption 情報リストを返す。

    R4: 1shot=1telop 制約を撤廃。同一 shot に載る参考テロップを最大
    _MAX_TELOPS_PER_SHOT 件まで保持する。超過分は confidence 降順で切る。
    先頭要素は「skeleton の代表テロップ」として caption_in_offset_sec/
    caption_out_offset_sec/telop_style_hint の legacy キーへも投影される
    （後方互換: 既存の sfx_planner / subtitles / premiere は先頭要素を読む）。

    Returns:
        dict shot_id -> {
            # legacy（先頭 caption のエイリアス。後方互換）
            "caption_in_offset_sec": float,
            "caption_out_offset_sec": float,
            "telop_style_hint": dict,
            # R4: 全 caption（0 <= n <= _MAX_TELOPS_PER_SHOT）
            "captions": [
                {"caption_in_offset_sec": float, "caption_out_offset_sec": float,
                 "telop_style_hint": dict, "confidence": float},
                ...
            ],
        }
    """
    # まず shot_id -> [caption dict, ...] を confidence 付きで集める
    per_shot: dict = {}
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
        try:
            conf = float(tel.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        per_shot.setdefault(shot_id, []).append({
            "caption_in_offset_sec": caption_in,
            "caption_out_offset_sec": caption_out,
            "telop_style_hint": hint,
            "confidence": conf,
            "_seq": len(per_shot.get(shot_id, [])),  # 元順序保存（同点タイブレーク用）
        })

    out: dict = {}
    for shot_id, entries in per_shot.items():
        # confidence 降順で並べ、上限 _MAX_TELOPS_PER_SHOT 個まで採用。
        # 同 confidence は元順序（先頭ほど早い）を保つ。
        entries.sort(key=lambda x: (-float(x.get("confidence") or 0.0), int(x.get("_seq") or 0)))
        picked = entries[:_MAX_TELOPS_PER_SHOT]
        # 最終出力は時系列順（caption_in_offset_sec 昇順）に並べ直す
        picked.sort(key=lambda x: float(x.get("caption_in_offset_sec") or 0.0))
        captions = []
        for p in picked:
            captions.append({
                "caption_in_offset_sec": float(p["caption_in_offset_sec"]),
                "caption_out_offset_sec": float(p["caption_out_offset_sec"]),
                "telop_style_hint": dict(p["telop_style_hint"]),
            })
        head = captions[0]
        out[shot_id] = {
            "caption_in_offset_sec": head["caption_in_offset_sec"],
            "caption_out_offset_sec": head["caption_out_offset_sec"],
            "telop_style_hint": dict(head["telop_style_hint"]),
            "captions": captions,
        }
    return out


def select_salient_onsets(sfx_events, max_count,
                           cluster_gap_sec=_SALIENT_CLUSTER_GAP_SEC,
                           kind_weight=None):
    """R3: 参考の sfx_events から「顕著オンセット」だけを選ぶ純関数。

    「顕著」の3基準:
      1. kind 優先度（transition/impact/riser > pop > shimmer。_SALIENT_KIND_WEIGHT）
      2. confidence（強いほど採用されやすい。weight = confidence * kind_weight[kind]）
      3. 時間的分離（`cluster_gap_sec` 内の連続オンセットは1クラスタに統合し、
         クラスタ内で weight が最大のものだけを代表として残す）

    抽出は「クラスタ代表→ weight 降順→上位 `max_count` を取る」の順で行い、
    最終的に t 昇順で返す（kind 未マップ / kind=other は除外）。

    Args:
        sfx_events: reference_spec v2 の sfx_events（各要素 dict）
        max_count: 抽出上限（>=1）。1未満は 1 に丸める。
        cluster_gap_sec: クラスタ判定の隣接オンセットギャップ（秒）。
        kind_weight: kind ごとの重み dict。省略時は _SALIENT_KIND_WEIGHT。

    Returns:
        List[dict]。各要素 {"t": float, "kind": str, "confidence": float, "weight": float}。
        t 昇順。sfx_events が空 / 全て無効なら空リスト。

    サブ意図: この関数は qa/fidelity.py と director.build_shot_skeleton の両方から
    呼ばれる（メトリクスの denom と plan の SE 選定を同じ集合で揃えるため）。
    """
    if not sfx_events:
        return []
    if not isinstance(max_count, int) or max_count < 1:
        max_count = max(1, int(max_count or 1))
    weight_map = dict(_SALIENT_KIND_WEIGHT if kind_weight is None else kind_weight)

    # 1. 有効なイベント（kind マップあり・t 数値）だけ拾い、weight を計算
    filtered = []
    for ev in sfx_events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if kind not in _SFX_KIND_TO_FAMILY:
            continue  # "other" 等はここで除外
        t = ev.get("t")
        if not isinstance(t, (int, float)):
            continue
        conf = float(ev.get("confidence") or 0.0)
        w = float(weight_map.get(kind, 0.6)) * max(0.0, conf)
        if w <= 0:
            continue
        filtered.append({"t": float(t), "kind": kind, "confidence": conf, "weight": w})
    if not filtered:
        return []

    # 2. t 昇順で走査して「隣接ギャップ < cluster_gap_sec」ならクラスタに追加、
    #    そうでなければ新クラスタを開始。各クラスタから weight 最大の代表 1 個を選ぶ。
    filtered.sort(key=lambda e: e["t"])
    clusters = []
    current = [filtered[0]]
    for ev in filtered[1:]:
        if ev["t"] - current[-1]["t"] < float(cluster_gap_sec):
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    clusters.append(current)

    representatives = []
    for cl in clusters:
        # weight 最大。同点は kind 優先度で決着（transition > impact > riser > pop > shimmer）
        pri = {"transition": 0, "impact": 1, "riser": 2, "pop": 3, "shimmer": 4}
        cl_sorted = sorted(cl, key=lambda e: (-e["weight"], pri.get(e["kind"], 9), e["t"]))
        representatives.append(cl_sorted[0])

    # 3. representatives を weight 降順で上位 max_count 個
    representatives.sort(key=lambda e: (-e["weight"], e["t"]))
    picked = representatives[: int(max_count)]

    # 4. t 昇順で返す
    picked.sort(key=lambda e: e["t"])
    return picked


def _sfx_plan_from_salient_onsets(
    sfx_events, boundaries, shot_ids, scaled_durations,
    ref_duration, target_duration_sec, telops,
    telop_map=None,
):
    """R3: 顕著オンセットの実時刻をアンカーにして sfx_plan を組み立てる。

    旧 `_thin_sfx_events_for_shots` の「カット同期・confidence 降順で per-shot 制約」
    から、「顕著オンセット（参考の重要な音のみ）を参考時刻に忠実配置」へ差し替え。

    手順:
      1. `select_salient_onsets` で参考時間座標の顕著オンセットを最大 floor(target/2.5) 個抽出。
      2. 各オンセット t_ref を参考区間→ターゲット区間（scaled_durations）へ piecewise 線形写像。
         → post-beat_snap の target_shot_start を基準に t_target を算出（beat_snap との整合）。
      3. アンカー選定は「t_target に最も近いアンカー候補」で:
         - shot 境界（cut）: |t_target - target_shot_start[i]| <= _SFX_ANCHOR_SNAP_SEC
         - caption_in: shot に caption_in_offset_sec があり |t_target - caption_in_time| が
           shot_start より近く 0.3s 以内
         - それ以外: shot_start
      4. 1 shot あたり最大 _SKELETON_MAX_SFX_PER_SHOT 発の per-shot 上限を維持。

    family は各オンセットの kind から `_SFX_KIND_TO_FAMILY` で決定（旧実装と同じ）。
    """
    if not sfx_events or not shot_ids:
        return []
    global_limit = max(1, int(math.floor(target_duration_sec / _SKELETON_SFX_SEC_DIVISOR)))

    # target 空間の shot_start（beat_snap 済みの scaled_durations を積算）
    target_starts = [0.0]
    for d in scaled_durations:
        target_starts.append(target_starts[-1] + float(d))

    # 参考尺→target 尺の piecewise 写像（後で telop も同関数で写像する）
    def _ref_to_target(t_ref):
        tf = max(0.0, float(t_ref))
        n = len(shot_ids)
        for i in range(n):
            lo = boundaries[i]
            hi = boundaries[i + 1]
            if lo - 1e-6 <= tf < hi - 1e-6 or i == n - 1:
                seg_len = max(1e-3, hi - lo)
                rel = max(0.0, min(1.0, (tf - lo) / seg_len))
                return i, target_starts[i] + rel * float(scaled_durations[i])
        # 末尾超過（実質到達しないはず）
        return n - 1, target_starts[-1]

    # R3 codex-review 修正: caption_in アンカー候補は「実際に skeleton の shot に採用された
    # caption_in_offset_sec のある shot」のみに限定する。参考 telop は複数あっても
    # `_map_telops_to_shots` が同一shotの2枚目以降を捨てるため、捨てられた telop の start を
    # アンカーに使うと render 段の caption_in 実座標（採用された1枚目基準）とズレる。
    # (shot_index -> caption_in_time_target)
    caption_in_by_shot: Dict[int, float] = {}
    if telop_map:
        for sid, info in telop_map.items():
            if sid in shot_ids and isinstance(info, dict):
                ci_off = info.get("caption_in_offset_sec")
                if isinstance(ci_off, (int, float)):
                    idx = shot_ids.index(sid)
                    caption_in_by_shot[idx] = target_starts[idx] + float(ci_off)
    else:
        # 後方互換: telop_map が渡されない場合は参考 telop の start を全て候補にする（旧挙動）。
        for tel in telops or []:
            if isinstance(tel, dict) and isinstance(tel.get("start"), (int, float)):
                idx, t_tg = _ref_to_target(tel["start"])
                # 同じ shot に複数 telop があった場合、先頭を残す（skeleton 側の挙動と揃える）
                caption_in_by_shot.setdefault(idx, t_tg)

    salient = select_salient_onsets(sfx_events, max_count=global_limit)

    per_shot_count = {sid: 0 for sid in shot_ids}
    result = []
    for ev in salient:
        kind = ev["kind"]
        family = _SFX_KIND_TO_FAMILY.get(kind)
        if not family:
            continue
        i, t_target = _ref_to_target(ev["t"])
        shot_id = shot_ids[i]
        if per_shot_count[shot_id] >= _SKELETON_MAX_SFX_PER_SHOT:
            continue

        shot_start = target_starts[i]
        shot_dur = float(scaled_durations[i])
        # アンカー候補: shot_start / caption_in / cut(=shot境界).
        # cut は本質的に shot_start と同時刻なので、shot境界に極めて近い(<snap) 場合に "cut" を採用。
        anchor_type = "shot_start"
        anchor_time = shot_start
        # (a) shot 境界（cut）
        dist_to_boundary = abs(t_target - shot_start)
        if dist_to_boundary <= _SFX_ANCHOR_SNAP_SEC:
            anchor_type = "cut"
            anchor_time = shot_start
        else:
            # (b) caption_in（この shot に採用された caption_in_offset_sec がある場合。
            #     R3 codex-review 修正: 参考 telop の全 start ではなく、実際に skeleton に
            #     採用された caption_in（telop_map の一枚目）だけを候補にする）
            ci_time = caption_in_by_shot.get(i)
            if ci_time is not None and ci_time <= t_target + 1e-6:
                ci_dist = abs(t_target - ci_time)
                if ci_dist <= 0.3 and ci_dist < dist_to_boundary:
                    anchor_type = "caption_in"
                    anchor_time = ci_time

        offset_sec = max(0.0, min(t_target - anchor_time, shot_dur))
        offset_sec = round(offset_sec, 3)

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


# 後方互換の別名（旧関数名を参照するテスト/コードに備える）
_thin_sfx_events_for_shots = _sfx_plan_from_salient_onsets


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


def _build_continuation_map(shot_ids, split_map, new_shot_ids):
    """分割された断片(2番目以降)の新 shot_id -> 分割群の先頭 shot_id を返す辞書。

    分割群の先頭以外の shot は「実装都合で切ったカット」であり、fidelity 側では
    親(先頭)に集約して評価する（continuation_of 経由）。
    """
    result: dict = {}
    for old_idx, old_sid in enumerate(shot_ids):
        if old_idx >= len(split_map):
            continue
        group = split_map[old_idx]
        if len(group) <= 1:
            continue
        head_new_sid = new_shot_ids[group[0]]
        for k in group[1:]:
            if 0 <= k < len(new_shot_ids):
                result[new_shot_ids[k]] = head_new_sid
    return result


def _remap_telops_by_split(telop_map, shot_ids, split_map, new_shot_ids, new_durations=None):
    """旧 shot_id -> {caption_in, caption_out, telop_style_hint, captions[...]} を分割後の shot に写像する。

    分割時、単一 telop（captions が 1 件以下）は分割群の**先頭のみ**に付与し、offset は
    先頭 shot の尺に合わせてクランプする（既存挙動）。
    R4 codex-review 修正 P1: 複数 telop（captions が 2 件以上）が旧 shot に載っている場合は、
    各 caption の start 時刻が属する分割断片に振り分ける（後半 caption を先頭断片へ潰さない）。
    振り分け後の各断片は、その断片内の相対 offset (=0..dur) を持つ。
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
        captions = info.get("captions") if isinstance(info.get("captions"), list) else None
        # 単一 telop（または captions が空/未指定）: 従来どおり先頭断片へ載せる
        if not captions or len(captions) <= 1:
            first_new_idx = new_group[0]
            new_sid = new_shot_ids[first_new_idx]
            remapped[new_sid] = dict(info)
            continue
        # 複数 telop: 各 caption を start が属する分割断片へ振り分ける
        # 断片開始オフセット（旧 shot 内座標）= 累積 new_durations
        durs = [float(new_durations[j]) for j in new_group] if new_durations else None
        # フォールバック: new_durations が無ければ全 caption を先頭断片へ（旧挙動）
        if durs is None:
            first_new_idx = new_group[0]
            new_sid = new_shot_ids[first_new_idx]
            remapped[new_sid] = dict(info)
            continue
        # 断片ごとの [start, end) 区間（旧 shot 座標）
        boundaries = [0.0]
        for d in durs:
            boundaries.append(boundaries[-1] + d)
        # 断片ごとに captions を集める
        per_fragment_caps: dict = {}
        for cap in captions:
            cin = float(cap.get("caption_in_offset_sec") or 0.0)
            cout = float(cap.get("caption_out_offset_sec") or cin)
            # start が属する断片を検索（境界と等しい時は左側優先）
            target_j = 0
            for j in range(len(durs)):
                if boundaries[j] - 1e-6 <= cin < boundaries[j + 1] - 1e-6 or j == len(durs) - 1:
                    target_j = j
                    break
            frag_start = boundaries[target_j]
            frag_dur = durs[target_j]
            local_in = max(0.0, min(cin - frag_start, frag_dur))
            local_out = max(local_in, min(cout - frag_start, frag_dur))
            new_sid = new_shot_ids[new_group[target_j]]
            per_fragment_caps.setdefault(new_sid, []).append({
                "caption_in_offset_sec": round(local_in, 3),
                "caption_out_offset_sec": round(local_out, 3),
                "telop_style_hint": dict(cap.get("telop_style_hint") or {}),
            })
        # 各断片について info を組み立てる（先頭 caption を legacy キーへ）
        for new_sid, caps in per_fragment_caps.items():
            caps.sort(key=lambda c: c["caption_in_offset_sec"])
            head = caps[0]
            remapped[new_sid] = {
                "caption_in_offset_sec": head["caption_in_offset_sec"],
                "caption_out_offset_sec": head["caption_out_offset_sec"],
                "telop_style_hint": dict(head["telop_style_hint"]),
                "captions": caps,
            }
    return remapped


def _remap_sfx_by_split(sfx_plan, shot_ids, new_shot_ids, split_map, new_durations,
                         old_telop_map=None, new_telop_map=None):
    """sfx_plan の t_anchor.shot_id / offset_sec を分割後の shot 群に写像する。

    R3 codex-review 修正: caption_in アンカーの offset は「caption 開始からの相対」で
    あり、shot_start からの相対ではない。分割時にこれを混同すると、旧 shot 後半の
    caption 付近のSEが分割後の先頭断片へ誤って移動して数秒早く鳴ってしまう。

    そこで、まず anchor 型に応じて「旧 shot 内の絶対時刻（shot_start からの位置）」に
    解決 → 分割後の断片位置を求める → 新 shot に対する anchor 型に応じた offset を計算する。

    - anchor_type="cut" / "shot_start" → in_shot_pos = offset_sec
    - anchor_type="caption_in"        → in_shot_pos = old_caption_in_off + offset_sec

    分割後の新 shot でも、telop_map は分割群の先頭断片にだけ載る（_remap_telops_by_split）。
    そのため caption_in を維持できるのは「新 shot が旧 shot の先頭断片」で telop が残る場合のみ。
    それ以外は shot_start にフォールバックする（意味論的に一貫）。
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
        atype = anc.get("type") or "shot_start"

        # ステップ1: 旧 shot 内の絶対位置（shot_start からの秒）を解決
        if atype == "caption_in" and old_telop_map:
            old_ci = 0.0
            info = (old_telop_map or {}).get(old_sid)
            if isinstance(info, dict) and isinstance(info.get("caption_in_offset_sec"), (int, float)):
                old_ci = float(info["caption_in_offset_sec"])
            in_shot_pos = old_ci + offset
        else:
            in_shot_pos = offset

        # ステップ2: 分割群内で in_shot_pos が属する断片を判定
        cursor = 0.0
        target_new_idx = group[0]
        target_pos_in_new_shot = in_shot_pos
        for j, new_idx in enumerate(group):
            dur = new_durations[new_idx]
            if in_shot_pos <= cursor + dur + 1e-6 or j == len(group) - 1:
                target_new_idx = new_idx
                target_pos_in_new_shot = max(0.0, in_shot_pos - cursor)
                target_pos_in_new_shot = min(target_pos_in_new_shot, dur)
                break
            cursor += dur

        # ステップ3: 新 shot に載る anchor 型を決める
        new_sid = new_shot_ids[target_new_idx]
        new_atype = atype
        new_offset = target_pos_in_new_shot
        if atype == "caption_in":
            # 新 shot に caption_in が残っていれば維持、無ければ shot_start にフォールバック
            new_ci_info = (new_telop_map or {}).get(new_sid)
            if isinstance(new_ci_info, dict) and isinstance(new_ci_info.get("caption_in_offset_sec"), (int, float)):
                new_ci = float(new_ci_info["caption_in_offset_sec"])
                new_offset = max(0.0, target_pos_in_new_shot - new_ci)
                new_offset = min(new_offset, new_durations[target_new_idx])
            else:
                new_atype = "shot_start"
                # new_offset はそのまま target_pos_in_new_shot（shot_start からの位置）

        new_ev = dict(ev)
        new_ev["t_anchor"] = dict(anc)
        new_ev["t_anchor"]["type"] = new_atype
        new_ev["t_anchor"]["shot_id"] = new_sid
        new_ev["t_anchor"]["offset_sec"] = round(new_offset, 3)
        remapped.append(new_ev)
    return remapped


def build_shot_skeleton(reference_spec, target_duration_sec, max_shot_sec=None, min_shot_sec=None,
                        beat_snap=False, beat_snap_tolerance_sec=0.25):
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
        beat_snap: R2a F5。True かつ reference_spec.music.beat_times が非空のとき、
            スケール後の shot 境界を最寄り拍時刻に吸着させる（±beat_snap_tolerance_sec）。
            吸着幅の上限は 0.25s 相当・min_shot_sec 保証と両立する。
        beat_snap_tolerance_sec: R2a F5。吸着幅上限。既定 0.25s。

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

    # R2a F5: ビートスナップ。target 空間のショット境界を、target 空間へスケールした
    # beat_times の最寄り拍へ吸着させる（±beat_snap_tolerance_sec、min_shot_sec保証）。
    # snap_boundaries_to_beats は端点(0/target)を保持するため、合計尺は不変。
    beat_snap_stats: Optional[Dict[str, int]] = None
    if beat_snap and snap_boundaries_to_beats is not None:
        music = reference_spec.get("music") or {}
        beat_times_ref = music.get("beat_times") or []
        if beat_times_ref:
            if ref_duration > 0:
                ratio = float(target_duration_sec) / float(ref_duration)
                beat_times_tgt = [float(t) * ratio for t in beat_times_ref]
            else:
                beat_times_tgt = [float(t) for t in beat_times_ref]
            tgt_boundaries = [0.0]
            for d in scaled:
                tgt_boundaries.append(round(tgt_boundaries[-1] + float(d), 4))
            # 末端は target_duration_sec に固定（浮動小数誤差を潰す）。
            tgt_boundaries[-1] = float(target_duration_sec)
            snapped, beat_snap_stats = snap_boundaries_to_beats(
                tgt_boundaries, beat_times_tgt,
                tolerance_sec=float(beat_snap_tolerance_sec),
                min_shot_sec=float(effective_min_shot_sec),
            )
            new_scaled = [round(snapped[i + 1] - snapped[i], 3) for i in range(len(snapped) - 1)]
            # 合計尺の微小誤差を最終ショットで吸収して target_duration_sec に一致させる。
            drift = float(target_duration_sec) - sum(new_scaled)
            if abs(drift) > 1e-6 and new_scaled:
                new_scaled[-1] = round(new_scaled[-1] + drift, 3)
            scaled = new_scaled

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
    # R3 codex-review 修正: telop_map を渡して「skeleton に採用された caption_in」だけを
    # アンカー候補にする（同一shotの2枚目以降 telop の start で誤アンカーを回避）。
    sfx_plan = _sfx_plan_from_salient_onsets(
        reference_spec.get("sfx_events") or [],
        boundaries,
        shot_ids,
        scaled,
        ref_duration,
        float(target_duration_sec),
        reference_spec.get("telops") or [],
        telop_map=telop_map,
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
    continuation_map: Dict[str, str] = {}
    if max_shot_sec is not None and max_shot_sec > 0:
        new_durations, split_map = _split_long_shots(scaled, float(max_shot_sec), min_shot_sec=effective_min_shot_sec)
        new_shot_ids = ["s{}".format(i + 1) for i in range(len(new_durations))]
        # R4: 分割で生まれた 2番目以降の断片に continuation_of を張って fidelity 側で除外できるように。
        continuation_map = _build_continuation_map(shot_ids, split_map, new_shot_ids)
        # telop 写像を分割群の先頭 shot に載せ替え（旧 telop_map を先に保存してから remap）
        # R4 codex-review P1 修正: new_durations を渡して、複数テロップは start が属する断片へ振り分ける。
        old_telop_map = dict(telop_map)
        telop_map = _remap_telops_by_split(
            telop_map, shot_ids, split_map, new_shot_ids, new_durations=new_durations,
        )
        # reference_visual 写像を分割群の全員にコピー（同じ絵の一部を切り出したもの）
        rv_map = _remap_reference_visual_by_split(rv_map, shot_ids, split_map, new_shot_ids)
        # sfx 写像を分割断片に載せ替え
        # R3 codex-review 修正: caption_in アンカーの offset 意味論を保つため
        # old/new telop_map を渡す（無いと caption_in→shot_start の絶対位置解決を誤る）
        sfx_plan = _remap_sfx_by_split(
            sfx_plan, shot_ids, new_shot_ids, split_map, new_durations,
            old_telop_map=old_telop_map, new_telop_map=telop_map,
        )
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

    # R2b F7: narration_rhythm 参照（あれば各shotの目安文字数を計算）
    rhythm = reference_spec.get("narration_rhythm") or {}
    chars_per_sec = 0.0
    try:
        if isinstance(rhythm.get("chars_per_sec"), (int, float)):
            chars_per_sec = float(rhythm["chars_per_sec"])
    except Exception:
        chars_per_sec = 0.0

    shots = []
    for i, sid in enumerate(shot_ids):
        shot = {
            "id": sid,
            "duration_sec": float(scaled[i]),
        }
        if sid in continuation_map:
            # R4: 分割 2 番目以降の断片は continuation_of で親(先頭)を指す。
            shot["continuation_of"] = continuation_map[sid]
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
            # R4: 複数テロップが載る shot だけ caption_slots[] を追加する。
            # LLM が返す plan では captions[] (text 入り) を required とし、
            # skeleton には「構造(=何枚 / いつ〜いつ / どのスタイル)」のみを caption_slots[] で伝える。
            # 単一テロップ shot は従来どおり caption_jp + caption_in/out で扱う（後方互換）。
            raw_captions = info.get("captions") or []
            if len(raw_captions) > 1:
                slots = []
                for cap in raw_captions:
                    _cin = float(cap.get("caption_in_offset_sec") or 0.0)
                    _cout = float(cap.get("caption_out_offset_sec") or 0.0)
                    _cin = max(0.0, min(_cin, float(scaled[i])))
                    _cout = max(_cin, min(_cout, float(scaled[i])))
                    slots.append({
                        "caption_in_offset_sec": round(_cin, 3),
                        "caption_out_offset_sec": round(_cout, 3),
                        "telop_style_hint": dict(cap.get("telop_style_hint") or {}),
                    })
                shot["caption_slots"] = slots
        # F10: 参考映像情報を skeleton の各 shot に載せる
        if sid in rv_map:
            shot["reference_visual"] = rv_map[sid]
        # R2b F4: 参考 camera_move → plan.motion_preset を機械写像。
        # skeleton にこのフィールドを載せると _validate_plan_matches_skeleton が
        # 「plan がスケルトンと一致するか」の検査対象にし、LLM の作文余地が減る。
        rv = rv_map.get(sid) or {}
        cam = rv.get("camera_move") or rv.get("motion") or ""
        if camera_move_to_preset is not None:
            preset = camera_move_to_preset(cam)
        else:
            preset = "static"
        # motion_preset は plan_schema の MOTION_PRESETS 範囲内である前提（上の mapping で保証）。
        shot["motion_preset"] = preset
        # 参考の intensity（weak/strong）を保持（mock/higgsfield backend が読む）。
        intensity = rv.get("intensity")
        if isinstance(intensity, str) and intensity:
            shot["motion_intensity"] = intensity
        # R2b F7: 期待文字数（chars_per_sec × duration）。0 は載せない。
        if chars_per_sec > 0 and expected_chars_for_shot is not None:
            ec = expected_chars_for_shot(float(scaled[i]), chars_per_sec)
            if ec > 0:
                shot["expected_narration_chars"] = ec
        shots.append(shot)

    # R3 codex-review 修正 P2: 参考ショット境界 -> plan shot への対応（piecewise 写像）を
    # 後段の fidelity が使えるように、参考尺座標での「各生成 shot の参考範囲 [ref_start,
    # ref_end]」を meta 用に返す。境界マージ・分割が働いても正しい piecewise マップを
    # 再構築できる。
    # 元の boundaries は merge 後の参考尺配列（len(shot_ids)+1）を反映する。
    # 分割時: 分割群の先頭は 元 boundaries[i]、最後尾は元 boundaries[i+1] とし、
    # 中間は等分（ref 側は元区間を等分するのが自然な近似）で埋める。
    ref_ranges: List[Tuple[float, float]] = []
    if max_shot_sec is not None and max_shot_sec > 0:
        # 分割後の shot_ids は 分割群を平坦化したもの
        for old_idx in range(len(split_map)):
            group = split_map[old_idx]
            if not group:
                continue
            lo = boundaries[old_idx]
            hi = boundaries[old_idx + 1]
            m = len(group)
            if m == 1:
                ref_ranges.append((lo, hi))
            else:
                # 等分割（分割後の各 new_durations と同じ比率で参考区間を分ける）
                sub_durs = [new_durations[k] for k in group]
                total = sum(sub_durs) or 1.0
                cursor = lo
                for k, d in enumerate(sub_durs):
                    seg = (hi - lo) * (d / total)
                    ref_ranges.append((cursor, cursor + seg))
                    cursor += seg
    else:
        for i in range(len(shot_ids)):
            ref_ranges.append((boundaries[i], boundaries[i + 1]))

    return {
        "shots": shots,
        "sfx_plan": sfx_plan,
        "hook_end_shot_id": hook_end_shot_id,
        "cta_start_shot_id": cta_start_shot_id,
        # R3 P2: piecewise 写像用（fidelity が使う）— 各生成 shot に対応する参考尺の [start,end]
        "shot_ref_ranges": [(round(a, 4), round(b, 4)) for (a, b) in ref_ranges],
        # F7: 参考 rhythm を skeleton 経由で director プロンプトへ渡す
        "narration_rhythm": {
            "chars_per_sec": rhythm.get("chars_per_sec") if rhythm else 0.0,
            "avg_gap_sec": rhythm.get("avg_gap_sec") if rhythm else 0.0,
            "pause_points": (rhythm.get("pause_points") or []) if rhythm else [],
        } if rhythm else None,
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
        # R4: 分割断片(continuation_of)はスケルトンから plan にそのまま持ち越す必要がある。
        # LLM が消してしまうと fidelity 側での continuation 除外が効かなくなる。
        if "continuation_of" in ss:
            pv = ps.get("continuation_of")
            sv = ss.get("continuation_of")
            if pv != sv:
                errors.append(
                    "shots[{}(id={})].continuation_of がスケルトンと一致しません"
                    "(plan={!r}, skeleton={!r}). スケルトン値をそのまま返してください.".format(
                        i, sid, pv, sv,
                    )
                )
        # R4: caption_slots[] （スケルトン側）と captions[] （plan 側）の件数一致。
        # skeleton は複数テロップ shot にだけ caption_slots[] を載せる。
        # plan は caption_slots に対応する captions[] を返し、各要素に text 必須。
        skel_slots = ss.get("caption_slots") if isinstance(ss.get("caption_slots"), list) else None
        if skel_slots:
            plan_caps = ps.get("captions") if isinstance(ps.get("captions"), list) else None
            if not isinstance(plan_caps, list) or len(plan_caps) != len(skel_slots):
                errors.append(
                    "shots[{}(id={})].captions 件数がスケルトンと不一致"
                    "(plan={}, skeleton={}件). スケルトンで指定されている caption の数を"
                    "厳密に返してください（複数テロップ対応 R4）。".format(
                        i, sid,
                        len(plan_caps) if isinstance(plan_caps, list) else "非リスト",
                        len(skel_slots),
                    )
                )
            else:
                for k, (pc, sc) in enumerate(zip(plan_caps, skel_slots)):
                    if not isinstance(pc, dict):
                        errors.append(
                            "shots[{}(id={})].captions[{}] はオブジェクトである必要があります".format(i, sid, k)
                        )
                        continue
                    for key in ("caption_in_offset_sec", "caption_out_offset_sec"):
                        pv = pc.get(key)
                        sv = sc.get(key)
                        if sv is None:
                            continue
                        if not isinstance(pv, (int, float)) or abs(float(pv) - float(sv)) > _SKELETON_CAPTION_MATCH_TOL:
                            errors.append(
                                "shots[{}(id={})].captions[{}].{} がスケルトンと不一致"
                                "(plan={}, skeleton={}, 許容±{}秒). スケルトン値をそのまま返してください.".format(
                                    i, sid, k, key, pv, sv, _SKELETON_CAPTION_MATCH_TOL,
                                )
                            )
                    tv = pc.get("text") or pc.get("caption_jp")
                    if not isinstance(tv, str) or not tv.strip():
                        errors.append(
                            "shots[{}(id={})].captions[{}].text が空です. 参考動画テロップの意図"
                            "に沿った日本語文言を必ず入れてください.".format(i, sid, k)
                        )
        # R2b F4: motion_preset の一致（スケルトン優先=機械判定の camera_move マッピング）
        if "motion_preset" in ss:
            pv = ps.get("motion_preset")
            sv = ss.get("motion_preset")
            if pv != sv:
                errors.append(
                    "shots[{}(id={})].motion_preset がスケルトンと一致しません(plan={!r}, skeleton={!r}). "
                    "スケルトンの motion_preset をそのまま採用してください（参考動画の光学フロー判定に"
                    "基づく機械マッピング結果）。".format(i, sid, pv, sv)
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

    # R3: telop 数（caption 有効 shot 数）がスケルトンと一致すること。
    # スケルトンで caption_in_offset_sec が付いている shot は「参考テロップが載る shot」
    # として選定済み。LLM が rewrite 段で任意に telop を追加/削除すると telop_iou が
    # 大きく崩れる（denom がずれる）ため、shot ID 単位で厳密一致を要求する。
    # R4.1 FIX (BUG-R4-01): 複数テロップ shot（caption_slots[]）は plan 側で captions[]
    # を使い caption_jp=""（プロンプト側でそう指示している）なので、caption_jp だけを
    # 数えると必ず missing 扱いになる。captions[] のいずれかに非空 text があれば
    # 「telop あり shot」としてカウントする。
    def _shot_has_telop(sh):
        cj = sh.get("caption_jp")
        if isinstance(cj, str) and cj.strip():
            return True
        caps = sh.get("captions")
        if isinstance(caps, list):
            for c in caps:
                if isinstance(c, dict):
                    tv = c.get("text") or c.get("caption_jp")
                    if isinstance(tv, str) and tv.strip():
                        return True
        return False

    skel_telop_ids = {s.get("id") for s in skel_shots if "caption_in_offset_sec" in s}
    plan_telop_ids = {s.get("id") for s in plan_shots if _shot_has_telop(s)}
    if skel_telop_ids != plan_telop_ids:
        missing = sorted(skel_telop_ids - plan_telop_ids)
        extra = sorted(plan_telop_ids - skel_telop_ids)
        parts = []
        if missing:
            parts.append("スケルトンで指定されているのに caption_jp が空: {}".format(missing))
        if extra:
            parts.append("スケルトンで指定されていない shot に caption_jp が入っている: {}".format(extra))
        errors.append(
            "telop 数（caption 有効 shot）がスケルトンと不一致(plan={}件, skeleton={}件). {} "
            "スケルトンで caption_in_offset_sec が付いている shot だけに caption_jp を入れ、"
            "それ以外の shot は caption_jp='' で返してください。telop 数はスケルトン固定です。".format(
                len(plan_telop_ids), len(skel_telop_ids), " / ".join(parts) if parts else "",
            )
        )

    # F13: narration_jp 文字数バジェット検査。
    # 参考動画のリズム(=カット割り)を再現するため、narration_jp を「shotの表示尺で音読しきれる」
    # 長さに拘束する。バジェットの基準は次の優先順位:
    #   1) shot.expected_narration_chars（build_shot_skeleton が参考動画の実測 cps で計算した値）
    #   2) skeleton.narration_rhythm.chars_per_sec × shot.duration_sec
    #   3) 既定 _NARRATION_DEFAULT_CHARS_PER_SEC(6.5字/秒) × shot.duration_sec
    #      （参考動画が transcript を持っていなくても最低限のガードを効かせる）
    rhythm = skeleton.get("narration_rhythm") or {}
    try:
        rhythm_cps = float(rhythm.get("chars_per_sec") or 0.0)
    except (TypeError, ValueError):
        rhythm_cps = 0.0
    effective_cps = rhythm_cps if rhythm_cps > 0 else _NARRATION_DEFAULT_CHARS_PER_SEC
    for i, (ps, ss) in enumerate(zip(plan_shots, skel_shots)):
        text = ps.get("narration_jp")
        if not isinstance(text, str) or not text.strip():
            # 空はここでは対象外（別の validator が narration_script 一致で担保）。
            continue
        max_chars = _narration_char_budget(ss, effective_cps)
        if max_chars is None:
            continue
        actual = len(text)
        if actual > max_chars:
            sid = ss.get("id")
            # rhythm_cps=0 のときはメッセージの根拠を「既定値」と明示する（0.00 字/秒 と誤表示しない）。
            budget_source = "expected_narration_chars" if ss.get("expected_narration_chars") else \
                "尺{:.2f}s × {:.2f}字/秒({})".format(
                    float(ss.get("duration_sec") or 0.0), effective_cps,
                    "参考動画実測" if rhythm_cps > 0 else "既定値",
                )
            errors.append(
                "shots[{}(id={})].narration_jp が文字数バジェットを超過しています"
                "(実{}字 > 上限{}字, 元は{} × +{:.0f}%許容). 短く書き直してください。"
                "冗長表現（『〜のように』『そして』『けれども』『さらに』『実は』『やはり』"
                "『ちなみに』の類）や重複するつなぎ言葉を削り、shot 尺内で音読できる長さに"
                "調整してください。この shot は参考動画のカット割りを再現するため尺を延ばせません。".format(
                    i, sid, actual, max_chars, budget_source,
                    _NARRATION_CHAR_BUDGET_TOLERANCE_RATIO * 100.0,
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
    # R4.1 FIX (BUG-R4-02): 実 LLM は prompt の出力スキーマ節に version/meta が明記
    # されていないと top-level に version を出さない（実測で確認）。meta と同じく
    # 機械的に復元可能なので validate 前に自動注入して無駄な矯正リトライを避ける。
    # skeleton モード(TTP)では v2 を、それ以外は v1 を採用する。
    # codex-review P2 (2026-07-25) 対応: 補完対象は「未指定(None)」のみに限定する。
    # LLM が明示的に `3` や `"2"` 等の不正値を出した場合はそのまま validate_plan に
    # 通し、矯正リトライで「version は [1, 2] のいずれか」を LLM に伝える（不正値を
    # 沈黙で書き換えると、後続の schema 拡張時に互換性の落とし穴になる）。
    if "version" not in candidate or candidate.get("version") is None:
        candidate["version"] = 2 if skeleton is not None else 1
    # R4.1 FIX (BUG-R4-03): 実 LLM が TTP モードで scene_id を勝手に振り、しかも
    # 「同一 scene 内の visual_prompt 一致」制約と参考映像(reference_visual)ごとに
    # 異なる visual_prompt を要求するルールが衝突する。TTP モードでは shot 境界と
    # 映像内容が skeleton で決まっているので、scene_id を強制的に落として plan_schema
    # の scene_id 一致検査を発火させない（後段レンダ・fidelity には無影響）。
    if skeleton is not None:
        for sh in candidate.get("shots") or []:
            if isinstance(sh, dict) and "scene_id" in sh:
                sh.pop("scene_id", None)

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
                  product=None, reference=None, checkpoint_dir=None, backend=None):
    """テーマ + 参考動画spec v2 から reel_plan を生成する。

    no_llm=True: claude 呼び出しを一切行わず、build_smoke_plan で最小plan を返す
    （テスト / スモーク用。本番企画には使わない）。
    reference: pipeline.reference_v2.analyze_reference_v2() が返す spec v2（dict）。
        指定必須（no_llm=False かつ call_claude_json が有効なとき）。無い場合は
        TTPReferenceRequiredError を送出する（--reference-url 必須の案内文つき）。
    quality: "supreme"（既定・write→polish の2段） / "single"（write のみ）。
    style: "default" / "vertical_hook"（プロンプトの書き分けのみ）。
    product: 商品アフィリエイト動画モード用の商品情報 {"name","url","image_count"}。
    checkpoint_dir: R2b申し送り対応。指定するとステージ間で中間 plan を JSON
        （plan_after_write.json / plan_after_polish.json / plan_after_rewrite.json）
        として保存する。3段直列の途中で失敗しても人手でリカバリできるように残す。

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

    # R2b申し送り対応: supreme_plus 用の1呼び出しタイムアウト延長。
    # 3段直列(write→polish→rewrite) × スケルトン矯正リトライ(最大3回) を最後まで走らせる
    # ため、既定10分では届かないケースがある。config で明示上書き可(claude_timeout_sec_supreme_plus)。
    # codex-review 指摘(P2): 明示指定が無い場合は「短縮しない」— 既存値と 1200 の最大値を採用する。
    # 明示的な claude_timeout_sec_supreme_plus はユーザー意思とみなしそのまま採用する。
    if quality == "supreme_plus":
        sp_explicit = config.get("claude_timeout_sec_supreme_plus")
        if sp_explicit is not None:
            sp_timeout = sp_explicit
        else:
            existing = config.get("claude_timeout_sec", 600)
            try:
                existing_num = float(existing)
            except (TypeError, ValueError):
                existing_num = 600.0
            sp_timeout = max(existing_num, 1200)  # 既存値が長ければそちらを維持
        config = dict(config)  # shallow copy — 元 config を汚さない
        config["claude_timeout_sec"] = sp_timeout

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
    # R4: backend が明示指定されており、かつ "higgsfield" 以外（mock/cloudapi）のときは
    # クレジット上限による分割を無効化する。mock はクレジット消費が無く、参考shotを
    # 分割すると fidelity の cut_match が「参考に無い分割境界」で構造的に目減りするため。
    # backend=None（明示なし）は従来挙動を保つ（後方互換）。
    hf_cfg = (config or {}).get("higgsfield") or {}
    max_shot_sec = None
    max_credits = hf_cfg.get("max_credits_per_shot")
    if isinstance(max_credits, (int, float)) and max_credits > 0:
        max_shot_sec = float(max_credits)
    if backend is not None and backend != "higgsfield":
        max_shot_sec = None
    # F12: config.director.min_shot_sec があれば skeleton の最小ショット尺として渡す
    # （supreme_plus プリセットで 0.5 に落として参考の高速カットを保持する）。
    director_cfg = (config or {}).get("director") or {}
    min_shot_sec_cfg = director_cfg.get("min_shot_sec")
    # R2a F5: config.director.beat_snap_enabled=True かつ reference.music.beat_times が
    # 非空のとき、shot 境界を最寄り拍にスナップする。
    # R2b申し送り対応: supreme_plus では既定 ON にする(明示的に False が設定されていな
    # い限り)。config.local/quality_max.json 未設定でも「品質最優先」意図を裏切らない。
    beat_snap_raw = director_cfg.get("beat_snap_enabled", None)
    if beat_snap_raw is None:
        beat_snap_enabled = (quality == "supreme_plus")
    else:
        beat_snap_enabled = bool(beat_snap_raw)
    beat_snap_tol_ms = director_cfg.get("beat_snap_tolerance_ms", 250)
    try:
        beat_snap_tol_sec = float(beat_snap_tol_ms) / 1000.0
    except (TypeError, ValueError):
        beat_snap_tol_sec = 0.25
    skeleton = build_shot_skeleton(
        reference, target_duration_sec, max_shot_sec=max_shot_sec,
        min_shot_sec=min_shot_sec_cfg if isinstance(min_shot_sec_cfg, (int, float)) and min_shot_sec_cfg > 0 else None,
        beat_snap=beat_snap_enabled,
        beat_snap_tolerance_sec=beat_snap_tol_sec,
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

    # R2b申し送り対応: ステージ中間 plan を checkpoint_dir に保存する薄いヘルパ。
    # 例外は握って呼び出し側に伝播させない（保存失敗で本流を止めない）。
    def _save_checkpoint(name, obj):
        if not checkpoint_dir or obj is None:
            return
        try:
            import pathlib as _pl
            _p = _pl.Path(str(checkpoint_dir))
            _p.mkdir(parents=True, exist_ok=True)
            (_p / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    write_trace = {}
    prompt = build_director_prompt(
        theme, target_duration_sec, target_tolerance_sec, style=style, angle_block="", product=product,
        reference_block=reference_block,
    )
    write_start = time.monotonic()
    plan = _attempt_plan(
        prompt, config, MAX_RETRIES, target_duration_sec, target_tolerance_sec,
        trace=write_trace, skeleton=skeleton, reference=reference,
    )
    stages["write"] = {
        "ok": plan is not None,
        "model_used": write_trace.get("last_model_used"),
        "elapsed_sec": round(time.monotonic() - write_start, 3),
    }
    _save_checkpoint("plan_after_write.json", plan)

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
        polish_start = time.monotonic()
        polished = _attempt_plan(
            critique_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec,
            trace=polish_trace, skeleton=skeleton, reference=reference,
        )
        stages["polish"] = {
            "ok": polished is not None,
            "model_used": polish_trace.get("last_model_used"),
            "elapsed_sec": round(time.monotonic() - polish_start, 3),
        }
        if polished is not None:
            plan = polished
        # polish 不合格: write のドラフトをそのまま採用。
        _save_checkpoint("plan_after_polish.json", plan)

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
            "**telop 数（caption_jp を持つ shot 数）は絶対に変えないこと**: ドラフトで caption_jp が"
            "入っている shot 集合とスケルトンで caption_in_offset_sec が指定されている shot 集合は"
            "完全一致していなければならない。rewrite では caption_jp の文言のみを推敲し、caption を"
            "新たに追加/削除しないこと（追加/削除は不合格の原因になる）。\n\n"
            "**narration_jp 文字数バジェット厳守（F13）**: 各 shot の `narration_jp` は "
            "`expected_narration_chars`（無ければ 尺×既定 6.5字/秒）× **+15% までが絶対上限**。"
            "超過は不合格。表現の質を上げるとき、**長くしないこと**（短くする方向のみ許可）。"
            "冗長表現（『〜のように』『そして』『けれども』『さらに』『実は』『やはり』"
            "『ちなみに』『〜という感じで』）と重複するつなぎ言葉を削り、断定・体言止め・"
            "短い動詞句で書き直すこと。文が長いと TTS 実尺が伸び、参考動画のカット割りが崩れる。\n\n"
            "ドラフト:\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n\n---\n\n" + rewrite_prompt
        )
        rewrite_start = time.monotonic()
        rewritten = _attempt_plan(
            rewrite_prompt, config, POLISH_MAX_RETRIES, target_duration_sec, target_tolerance_sec,
            trace=rewrite_trace, skeleton=skeleton, reference=reference,
        )
        stages["rewrite"] = {
            "ok": rewritten is not None,
            "model_used": rewrite_trace.get("last_model_used"),
            "elapsed_sec": round(time.monotonic() - rewrite_start, 3),
        }
        if rewritten is not None:
            plan = rewritten
        _save_checkpoint("plan_after_rewrite.json", plan)

    plan.setdefault("meta", {})
    plan["meta"]["style"] = style
    plan["meta"]["quality"] = quality
    plan["meta"]["product"] = (product or {}).get("name") if product else None
    if quality in ("supreme", "supreme_plus"):
        plan["meta"]["stages"] = stages
    # 参考spec の要点を meta に記録（下流の render/検証で使える）
    plan["meta"]["reference_skeleton_shot_count"] = len(skeleton["shots"])
    plan["meta"]["reference_skeleton_sfx_count"] = len(skeleton.get("sfx_plan") or [])
    # R3: piecewise 写像用の参考尺 [start, end] 対応を meta に持たせる
    # （qa/fidelity.py が boundary merge / shot split の場合でも piecewise IoU を可能にする）
    if skeleton.get("shot_ref_ranges"):
        plan["meta"]["shot_ref_ranges"] = skeleton["shot_ref_ranges"]
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
