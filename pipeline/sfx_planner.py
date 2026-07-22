# -*- coding: utf-8 -*-
"""plan v2 の sfx_plan（映像を意識したSE配置）を実レンダリング時刻へ解決する純関数群。

TTP v2 Phase 3 の中核。従来 render.compute_cut_sfx_events() は「ショット境界の
累積時刻に機械的にSE配置」だったが、これでは映像の意味（フック終端・キャプション出現・
CTA開始）とSE位置が一致しない。

plan v2 では director が `sfx_plan` として:
  {"t_anchor": {"type": "cut"|"caption_in"|"shot_start", "shot_id": str, "offset_sec": float},
   "family": "whoosh"|"impact"|"riser"|"pop"|"shimmer",
   "gain_db": float(任意)}
のイベント列を出力する。sfx_planner はこれを:
  1. shot_display_durations（**実際に確定した各shotの表示尺**）から実測境界を計算し、
  2. アンカータイプに応じて実時刻を解決し、
  3. family→ファイル選定を pipeline.edit_profile.pick_cut_sfx() で決定論的に行い、
  4. min_interval で間引き、
  5. mean_volume_db を manifest から propagate する。

**設計上の重要な要点**: shot_display_durations は plan の名目 duration_sec ではなく
「音声同期モードで pad された最終表示尺」を渡すこと。これにより、shot 尺が変動しても
plan.sfx_plan の相対位置は保たれ、SE が字幕/カットと同期し続ける。

I/O 禁止（manifest は呼び出し側が渡す）。Python 3.9 互換構文のみ。
"""
from __future__ import annotations


DEFAULT_MIN_INTERVAL_SEC = 1.5
# BUG-53: 従来 -18dB は最終ミックス後のRMSベースラインとの差分が +1dB 未満に埋もれていた。
# ミックス経路変更(loudnorm後段でSFXオーバーレイ) + BGMダッキング(SE時刻±0.3sで-4dB) と
# 組み合わせて、SEのピークがベースライン+3dB以上を安定して超えるようにする狙いで -6dB に
# 引き上げる(実聴感で -10〜-6dB 目安)。edit_profile 側の cut_sfx.gain_db 明示指定はそちらを優先。
DEFAULT_GAIN_DB = -6


def _shot_index_by_id(shots):
    """plan["shots"] 相当の配列から shot_id -> index の辞書を作る。"""
    return {s.get("id"): i for i, s in enumerate(shots or []) if isinstance(s, dict) and s.get("id")}


def _boundaries_from_durations(durations):
    """各ショット表示尺のリストから、累積境界（各ショット開始時刻）配列を返す。

    例: [2.0, 3.0, 4.0] -> [0.0, 2.0, 5.0]（長さは len(durations) = 3）。
    """
    boundaries = []
    cursor = 0.0
    for d in durations or []:
        boundaries.append(cursor)
        cursor += float(d)
    return boundaries


def resolve_hook_cta_bounds(plan, shot_display_durations):
    """plan.hook_end_shot_id / plan.cta_start_shot_id を実時刻へ解決する。

    hook_end_sec = そのショットの表示終了時刻（次ショット開始時刻）。
    cta_start_sec = そのショットの表示開始時刻。

    どちらも該当 shot_id が plan.shots に無い / 未指定なら None を返す。
    shot_display_durations の長さが plan.shots と一致しない場合は最短側に合わせる
    （呼び出し側での取り違えを吸収）。

    Returns: (hook_end_sec: float|None, cta_start_sec: float|None)
    """
    if not isinstance(plan, dict):
        return None, None
    shots = plan.get("shots") or []
    durations = list(shot_display_durations or [])
    idx_by_id = _shot_index_by_id(shots)
    boundaries = _boundaries_from_durations(durations)
    n = min(len(durations), len(shots))

    def _resolve_end(shot_id):
        if not shot_id or shot_id not in idx_by_id:
            return None
        i = idx_by_id[shot_id]
        if i >= n:
            return None
        return boundaries[i] + float(durations[i])

    def _resolve_start(shot_id):
        if not shot_id or shot_id not in idx_by_id:
            return None
        i = idx_by_id[shot_id]
        if i >= n:
            return None
        return boundaries[i]

    hook_end_sec = _resolve_end(plan.get("hook_end_shot_id"))
    cta_start_sec = _resolve_start(plan.get("cta_start_shot_id"))
    return hook_end_sec, cta_start_sec


def _resolve_anchor_absolute_sec(anchor_type, shot_start, shot_dur, offset_sec, caption_in_offset_sec):
    """1イベントの t_anchor を絶対秒へ解決する内部ヘルパ。

    - type="cut" or "shot_start": shot_start + offset_sec
    - type="caption_in": shot_start + (caption_in_offset_sec or 0.0) + offset_sec
    どのタイプでも [shot_start, shot_start + shot_dur] へクランプはしない
    （呼び出し側で shot_dur を実測境界にしているため、offset_sec は plan schema バリデーションを
     通過している前提。極端な逸脱はテスト側で捕捉できるようにする）。
    """
    off = float(offset_sec or 0.0)
    if anchor_type == "caption_in":
        return float(shot_start) + float(caption_in_offset_sec or 0.0) + off
    # "cut" / "shot_start" / それ以外（safety fallback）
    return float(shot_start) + off


def _thin_by_min_interval(events, min_interval_sec):
    """時刻昇順のイベント列を min_interval_sec 未満の連続分を間引く。

    events: [(at_sec, source_ev_dict), ...] 前提。at_sec は解決済み絶対秒。
    """
    kept = []
    last = None
    for at_sec, src in events:
        if last is not None and (at_sec - last) < float(min_interval_sec):
            continue
        kept.append((at_sec, src))
        last = at_sec
    return kept


def resolve_sfx_events(
    plan,
    shot_display_durations,
    edit_profile=None,
    manifest=None,
    project_seed=None,
    min_interval_sec=DEFAULT_MIN_INTERVAL_SEC,
    default_gain_db=DEFAULT_GAIN_DB,
):
    """plan.sfx_plan を build_final_cmd の sfx 引数へ渡せる形へ解決する（純関数）。

    Args:
        plan: pipeline.plan_schema.validate_plan() で normalize 済みの plan dict。
              少なくとも "shots"（各要素に id / duration_sec / 任意で caption_in_offset_sec）と
              "sfx_plan" を持つこと。sfx_plan が None/空なら空リストを返す。
        shot_display_durations: **実測**の各ショット表示尺のリスト（plan の名目 duration_sec
              ではなく、同期モード等で確定した尺）。長さは plan["shots"] と一致していること。
              長さが違う場合は短い方に合わせる（安全側フォールバック）。
        edit_profile: pipeline.edit_profile.load_edit_profile() の戻り値（任意）。
              渡された場合、cut_sfx.gain_db を default_gain_db として、cut_sfx.manifest を
              manifest として使う（引数 manifest 明示指定があればそちらを優先）。
        manifest: pipeline.edit_profile.load_sfx_manifest() の戻り値（任意）。
              渡されない場合は edit_profile.cut_sfx.manifest を使う。どちらも無ければ
              ファイル解決が None になり、対象イベントはスキップされる。
        project_seed: pick_cut_sfx の決定論シード。
        min_interval_sec: 隣接SEの最小間隔（この間隔未満は間引く）。
        default_gain_db: イベントに gain_db 指定が無いときのフォールバック値。

    Returns:
        list[dict]: [{"path": str, "at_sec": float, "gain_db": float,
                       "mean_volume_db": float?}, ...] を at_sec 昇順で返す。
    """
    if not isinstance(plan, dict):
        return []
    sfx_plan = plan.get("sfx_plan")
    if not sfx_plan:
        return []

    shots = plan.get("shots") or []
    idx_by_id = _shot_index_by_id(shots)
    durations = [float(d) for d in (shot_display_durations or [])]
    boundaries = _boundaries_from_durations(durations)
    n = min(len(durations), len(shots))

    # edit_profile 側の設定を吸収（cut_sfx.gain_db / cut_sfx.manifest）
    ep_cut = {}
    if isinstance(edit_profile, dict):
        ep_cut = edit_profile.get("cut_sfx") or {}
    if manifest is None:
        manifest = ep_cut.get("manifest") or []
    resolved_default_gain = ep_cut.get("gain_db")
    if resolved_default_gain is None:
        resolved_default_gain = default_gain_db

    # 1. アンカー解決（時刻昇順に並べる）
    resolved = []
    for ev in sfx_plan:
        if not isinstance(ev, dict):
            continue
        anc = ev.get("t_anchor") or {}
        shot_id = anc.get("shot_id")
        if shot_id not in idx_by_id:
            continue
        i = idx_by_id[shot_id]
        if i >= n:
            continue
        shot = shots[i] if i < len(shots) else {}
        shot_start = boundaries[i]
        shot_dur = durations[i]
        anchor_type = anc.get("type")
        caption_in = shot.get("caption_in_offset_sec")
        offset_sec = anc.get("offset_sec", 0.0)
        at_sec = _resolve_anchor_absolute_sec(
            anchor_type, shot_start, shot_dur, offset_sec, caption_in
        )
        if at_sec < 0.0:
            at_sec = 0.0
        resolved.append((at_sec, ev))

    if not resolved:
        return []

    resolved.sort(key=lambda kv: kv[0])

    # 2. min_interval で間引き
    kept = _thin_by_min_interval(resolved, min_interval_sec)
    if not kept:
        return []

    # 3. family→ファイル選定（決定論・連続同音回避）
    # pipeline.edit_profile の pick_cut_sfx / resolve_sfx_file_path を再利用する。
    # 遅延importで循環を避ける（sfx_planner はドメイン純粋 / edit_profile は I/O 込み）。
    from pipeline import edit_profile as _ep

    specs = []
    last_file = None
    for idx, (at_sec, ev) in enumerate(kept):
        family = ev.get("family")
        if not family:
            continue
        family_weights = {family: 1}
        picked = None
        if manifest:
            picked = _ep.pick_cut_sfx(
                seed=project_seed, index=idx, manifest=manifest,
                family_weights=family_weights, avoid_file=last_file,
            )
        if picked is None:
            # manifest 未提供 or 家族該当なし → このイベントはスキップ
            # （旧経路は resolved_path 固定にフォールバックしたが、plan v2 経路では
            #  「特定family指定」の要求を満たせない場合はSE無しの方が安全＝映像の
            #  意味とSEが食い違わない）。
            continue
        picked_path = _ep.resolve_sfx_file_path(picked.get("file"))
        if not picked_path:
            continue

        gain_db = ev.get("gain_db")
        if gain_db is None:
            gain_db = resolved_default_gain

        spec = {
            "path": picked_path,
            "at_sec": float(at_sec),
            "gain_db": float(gain_db),
        }
        mean_volume_db = picked.get("mean_volume_db")
        if mean_volume_db is not None:
            spec["mean_volume_db"] = mean_volume_db
        specs.append(spec)
        last_file = picked.get("file")

    return specs
