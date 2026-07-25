# -*- coding: utf-8 -*-
"""reel_plan（テーマ→9:16リール企画）のバリデーション・正規化・スモーク代替生成。

video-auto-editor/pipeline/plan_schema.py のパターン（validate_plan が (ok, errors,
normalized_plan) を返す・エラーは矯正リトライへ差し戻す）を踏襲しつつ、対象が
「編集判断(keep_segments)」ではなく「ゼロから作る企画(コンセプト/フック/台本/ショットリスト)」
である点に合わせてスキーマを作り直した。

plan スキーマ（v1: 従来互換 / v2: TTP参考動画からのスケルトン写像モード）:
{
  "version": 1 | 2,
  "meta": {"source": "ai"|"rule"|"smoke", "model_used": str|None, "fallback_from": str|None,
            "fallback_reason": str|None, "attempt": int},
  "concept": str,
  "hook": str,
  "narration_script": str,           # 日本語ナレーション全文
  "shots": [
    {
      "id": str,
      "visual_prompt": str,          # 英語、text-to-video/image-to-video向け
      "motion_preset": str,          # MOTION_PRESETS のいずれか
      "duration_sec": number,        # >0
      "caption_jp": str,             # 画面焼込キャプション（日本語）
      "narration_jp": str,           # 任意。このショットのナレーション断片（音声主導タイミング
                                      # 同期モード用。省略可＝後方互換）
      # 以下は v2(TTP) 拡張。すべて任意（後方互換）。
      "caption_in_offset_sec": number,   # ショット内でテロップが出現する相対秒（0 <= x <= duration_sec）
      "caption_out_offset_sec": number,  # ショット内でテロップが消滅する相対秒（in <= x <= duration_sec）
      "telop_style_hint": dict,          # 参考spec由来の position/color/size_class 等（描画側ヒント）
      # R4: 1shot=1telop 制約撤廃（複数テロップ対応）
      "captions": [                       # 任意。指定すると shot 内に複数テロップを載せる
        {"text": str,                     # 日本語テロップ本文
         "caption_in_offset_sec": number, # ショット内相対秒（0 <= x <= duration_sec）
         "caption_out_offset_sec": number,# 同上（in <= x <= duration_sec）
         "telop_style_hint": dict},       # 任意
        ...
      ],
      # R4: max_shot_sec 分割で生まれた 2 番目以降の断片は continuation_of で親を指す。
      # 参考動画の "分割前の shot" と結び付けるためのメタ情報。fidelity の cut_match で
      # 分割境界を「参考に無いカット」として計上しないための除外キーになる。
      "continuation_of": str,             # 任意（分割断片のみ）
    }, ...
  ],
  # v2 拡張: SFX配置（renderが実際に配線するのはP3担当）。
  "sfx_plan": [
    {
      "t_anchor": {"type": "cut"|"caption_in"|"shot_start", "shot_id": str, "offset_sec": number},
      "family": "whoosh"|"impact"|"riser"|"pop"|"shimmer",  # kind→familyマッピング済
      "gain_db": number,   # 任意
    }, ...
  ],
  "hook_end_shot_id": str,           # 任意。フック区間の終端ショットid
  "cta_start_shot_id": str,          # 任意。CTA区間の開始ショットid
  "bgm_mood": "upbeat"|"calm"|"emotional"|"none",
}

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

MOTION_PRESETS = ("static", "pan_left", "pan_right", "zoom_in", "zoom_out", "ken_burns")
BGM_MOODS = ("upbeat", "calm", "emotional", "none")

MIN_SHOT_DURATION = 0.4  # R2b F12: supreme_plus の高速カット参考（0.5s）を通すため 1.0 → 0.4 へ緩和。
MAX_SHOT_DURATION = 20.0
# テロップ最大文字数: PlayResX=1080 / MarginL=MarginR=60 / 既定フォントサイズ76px の実描画で
# 2行に収まる実効上限が「30字前後」であることをフレーム目視で確認した(BUG-54)。
# 30字超は subtitles.build_telop_pieces_from_shots が幅ベースでフォント縮小を試みるが、
# それでも見切れリスクが残るため、企画段階でハード上限とする。プロンプトには
# 「25字以内推奨」を明記して director が 25字前後を狙うようにする。
MAX_CAPTION_CHARS = 30

_SUPPORTED_PLAN_VERSIONS = (1, 2)
_SUPPORTED_SFX_FAMILIES = ("whoosh", "impact", "riser", "pop", "shimmer")
_SUPPORTED_SFX_ANCHOR_TYPES = ("cut", "caption_in", "shot_start")


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_plan(plan, target_duration_sec=None, target_tolerance_sec=8.0):
    """reel_plan を検査し、(ok, errors, normalized_plan) を返す。

    target_duration_sec を指定すると、shots の duration_sec 合計が
    target_duration_sec±target_tolerance_sec に収まっているかも検証する
    （video-auto-editor の keep_segments 合計チェックと同じ思想）。
    """
    errors = []

    if not isinstance(plan, dict):
        return False, ["plan はオブジェクト(dict)である必要があります"], None

    version = plan.get("version")
    if version not in _SUPPORTED_PLAN_VERSIONS:
        errors.append(
            "version は {} のいずれかである必要があります (got: {!r})".format(
                list(_SUPPORTED_PLAN_VERSIONS), version
            )
        )

    meta = plan.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta が存在しないかオブジェクトではありません")
        meta = {}
    elif meta.get("source") not in ("ai", "rule", "smoke"):
        errors.append("meta.source は 'ai' か 'rule' か 'smoke' である必要があります (got: {!r})".format(meta.get("source")))

    concept = plan.get("concept")
    if not isinstance(concept, str) or not concept.strip():
        errors.append("concept は非空文字列である必要があります")
        concept = ""

    hook = plan.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        errors.append("hook は非空文字列である必要があります")
        hook = ""

    # P0-2: narration_mode ∈ {present, absent, unknown}。任意フィールド（後方互換）。
    # "absent"（参考動画にナレーション無し）のときは narration_script の空文字を許容する
    # （BGM+テロップだけで語る TikTok 構成の忠実再現。TTS はスキップされる）。
    narration_mode = plan.get("narration_mode")
    if narration_mode is not None and narration_mode not in ("present", "absent", "unknown"):
        errors.append(
            "narration_mode は present/absent/unknown のいずれかである必要があります (got: {!r})".format(
                narration_mode
            )
        )
    narration = plan.get("narration_script")
    if narration_mode == "absent":
        # 参考にナレーション無し → narration は必ず空へ正規化する（非空でも黙って捨てる
        # のではなく、明示的に "" にして下流 TTS スキップと整合させる。codex-review High）。
        if narration is not None and not isinstance(narration, str):
            errors.append("narration_script は文字列である必要があります")
        narration = ""
    elif not isinstance(narration, str) or not narration.strip():
        errors.append("narration_script は非空文字列である必要があります")
        narration = ""

    shots_raw = plan.get("shots")
    normalized_shots = []
    if not isinstance(shots_raw, list) or not shots_raw:
        errors.append("shots は非空リストである必要があります")
    else:
        seen_ids = set()
        seen_scene_ids = set()
        prev_scene_id = None
        for i, shot in enumerate(shots_raw):
            if not isinstance(shot, dict):
                errors.append("shots[{}] はオブジェクトではありません".format(i))
                prev_scene_id = None
                continue
            sid = shot.get("id") or "s{}".format(i + 1)
            if sid in seen_ids:
                errors.append("shots の id が重複しています: {!r}".format(sid))
                prev_scene_id = None
                continue
            visual_prompt = shot.get("visual_prompt")
            motion_preset = shot.get("motion_preset", "static")
            duration_sec = shot.get("duration_sec")
            caption_jp = shot.get("caption_jp", "")
            narration_jp = shot.get("narration_jp")
            scene_id_raw = shot.get("scene_id")
            caption_in_off = shot.get("caption_in_offset_sec")
            caption_out_off = shot.get("caption_out_offset_sec")
            telop_style_hint = shot.get("telop_style_hint")
            # v2 拡張: reference_visual（参考動画の映像情報。F10）。任意・後方互換。
            #  {"desc_en": str, "framing": str, "location": str, "camera_move": str,
            #   "color_palette_hex": [str], "lighting": str, "shot_size": str,
            #   "subject_count": int, "color_mood": str} 等の辞書。
            reference_visual = shot.get("reference_visual")
            # R4: 複数テロップ用の captions[] と、分割断片印の continuation_of。
            captions_raw = shot.get("captions")
            continuation_of = shot.get("continuation_of")

            ok_item = True

            # scene_id は任意。指定された場合は非空文字列であること・同一 scene_id が
            # 連続ショット以外で再出現していないことを検証する。scene_id 無しは従来どおり
            # 単独ショット扱い（後方互換）。シーン尺・個数の制約はハードエラーにしない。
            scene_id = None
            if scene_id_raw is not None:
                if not isinstance(scene_id_raw, str) or not scene_id_raw.strip():
                    errors.append(
                        "shots[{}(id={})].scene_id は非空文字列である必要があります (got: {!r})".format(
                            i, sid, scene_id_raw
                        )
                    )
                    ok_item = False
                else:
                    scene_id = scene_id_raw.strip()
                    if scene_id != prev_scene_id and scene_id in seen_scene_ids:
                        errors.append(
                            "shots[{}(id={})].scene_id {!r} が連続しない位置で再利用されています"
                            "（同一 scene_id は連続ショットにのみ付けてください）".format(i, sid, scene_id)
                        )
                        ok_item = False
            prev_scene_id = scene_id
            if not isinstance(visual_prompt, str) or not visual_prompt.strip():
                errors.append("shots[{}(id={})].visual_prompt は非空文字列である必要があります".format(i, sid))
                ok_item = False
            if motion_preset not in MOTION_PRESETS:
                errors.append(
                    "shots[{}(id={})].motion_preset が不正です(got: {!r}, expected one of {})".format(
                        i, sid, motion_preset, MOTION_PRESETS
                    )
                )
                ok_item = False
            if not _is_number(duration_sec) or not (MIN_SHOT_DURATION <= duration_sec <= MAX_SHOT_DURATION):
                errors.append(
                    "shots[{}(id={})].duration_sec は{}〜{}の数値である必要があります (got: {!r})".format(
                        i, sid, MIN_SHOT_DURATION, MAX_SHOT_DURATION, duration_sec
                    )
                )
                ok_item = False
            if not isinstance(caption_jp, str):
                errors.append("shots[{}(id={})].caption_jp は文字列である必要があります".format(i, sid))
                ok_item = False
            elif len(caption_jp) > MAX_CAPTION_CHARS:
                errors.append(
                    "shots[{}(id={})].caption_jp が{}字を超えています: {!r}".format(
                        i, sid, MAX_CAPTION_CHARS, caption_jp
                    )
                )
                ok_item = False
            if narration_jp is not None and not isinstance(narration_jp, str):
                errors.append("shots[{}(id={})].narration_jp は文字列である必要があります".format(i, sid))
                ok_item = False

            # v2 拡張フィールド: caption_in_offset_sec / caption_out_offset_sec / telop_style_hint
            _dur = float(duration_sec) if _is_number(duration_sec) else None
            if caption_in_off is not None:
                if not _is_number(caption_in_off):
                    errors.append("shots[{}(id={})].caption_in_offset_sec は数値である必要があります".format(i, sid))
                    ok_item = False
                elif _dur is not None and not (-1e-6 <= float(caption_in_off) <= _dur + 1e-6):
                    errors.append(
                        "shots[{}(id={})].caption_in_offset_sec は 0〜duration_sec の範囲である必要があります "
                        "(got: {}, duration_sec: {})".format(i, sid, caption_in_off, _dur)
                    )
                    ok_item = False
            if caption_out_off is not None:
                if not _is_number(caption_out_off):
                    errors.append("shots[{}(id={})].caption_out_offset_sec は数値である必要があります".format(i, sid))
                    ok_item = False
                elif _dur is not None and not (-1e-6 <= float(caption_out_off) <= _dur + 1e-6):
                    errors.append(
                        "shots[{}(id={})].caption_out_offset_sec は 0〜duration_sec の範囲である必要があります "
                        "(got: {}, duration_sec: {})".format(i, sid, caption_out_off, _dur)
                    )
                    ok_item = False
                elif caption_in_off is not None and _is_number(caption_in_off) and float(caption_out_off) < float(caption_in_off) - 1e-6:
                    errors.append(
                        "shots[{}(id={})].caption_out_offset_sec は caption_in_offset_sec 以上である必要があります "
                        "(in={}, out={})".format(i, sid, caption_in_off, caption_out_off)
                    )
                    ok_item = False
            if telop_style_hint is not None and not isinstance(telop_style_hint, dict):
                errors.append("shots[{}(id={})].telop_style_hint はオブジェクトである必要があります".format(i, sid))
                ok_item = False
            if reference_visual is not None and not isinstance(reference_visual, dict):
                errors.append("shots[{}(id={})].reference_visual はオブジェクトである必要があります".format(i, sid))
                ok_item = False

            # R4: captions[] のバリデーション（任意フィールド）。
            #   各要素: {text, caption_in_offset_sec, caption_out_offset_sec, telop_style_hint?}
            normalized_captions = None
            if captions_raw is not None:
                if not isinstance(captions_raw, list):
                    errors.append("shots[{}(id={})].captions はリストである必要があります".format(i, sid))
                    ok_item = False
                else:
                    normalized_captions = []
                    for k, cap in enumerate(captions_raw):
                        if not isinstance(cap, dict):
                            errors.append("shots[{}(id={})].captions[{}] はオブジェクトである必要があります".format(i, sid, k))
                            ok_item = False
                            continue
                        c_text = cap.get("text")
                        if c_text is None:
                            c_text = cap.get("caption_jp")
                        c_in = cap.get("caption_in_offset_sec")
                        c_out = cap.get("caption_out_offset_sec")
                        c_hint = cap.get("telop_style_hint")
                        c_ok = True
                        if not isinstance(c_text, str) or not c_text.strip():
                            errors.append("shots[{}(id={})].captions[{}].text は非空文字列である必要があります".format(i, sid, k))
                            c_ok = False
                        elif len(c_text) > MAX_CAPTION_CHARS:
                            errors.append(
                                "shots[{}(id={})].captions[{}].text が{}字を超えています: {!r}".format(
                                    i, sid, k, MAX_CAPTION_CHARS, c_text,
                                )
                            )
                            c_ok = False
                        if c_in is not None and not _is_number(c_in):
                            errors.append("shots[{}(id={})].captions[{}].caption_in_offset_sec は数値".format(i, sid, k))
                            c_ok = False
                        elif c_in is not None and _dur is not None and not (-1e-6 <= float(c_in) <= _dur + 1e-6):
                            errors.append(
                                "shots[{}(id={})].captions[{}].caption_in_offset_sec は 0〜duration_sec の範囲(got {})".format(
                                    i, sid, k, c_in,
                                )
                            )
                            c_ok = False
                        if c_out is not None and not _is_number(c_out):
                            errors.append("shots[{}(id={})].captions[{}].caption_out_offset_sec は数値".format(i, sid, k))
                            c_ok = False
                        elif c_out is not None and _dur is not None and not (-1e-6 <= float(c_out) <= _dur + 1e-6):
                            errors.append(
                                "shots[{}(id={})].captions[{}].caption_out_offset_sec は 0〜duration_sec の範囲(got {})".format(
                                    i, sid, k, c_out,
                                )
                            )
                            c_ok = False
                        elif c_in is not None and c_out is not None and _is_number(c_in) and _is_number(c_out) and float(c_out) < float(c_in) - 1e-6:
                            errors.append(
                                "shots[{}(id={})].captions[{}].caption_out_offset_sec は caption_in_offset_sec 以上"
                                "(in={}, out={})".format(i, sid, k, c_in, c_out)
                            )
                            c_ok = False
                        if c_hint is not None and not isinstance(c_hint, dict):
                            errors.append("shots[{}(id={})].captions[{}].telop_style_hint は辞書である必要があります".format(i, sid, k))
                            c_ok = False
                        if c_ok:
                            entry = {"text": c_text.strip()}
                            if c_in is not None:
                                entry["caption_in_offset_sec"] = float(c_in)
                            if c_out is not None:
                                entry["caption_out_offset_sec"] = float(c_out)
                            if c_hint is not None:
                                entry["telop_style_hint"] = dict(c_hint)
                            normalized_captions.append(entry)
                        else:
                            ok_item = False

            if continuation_of is not None and (not isinstance(continuation_of, str) or not continuation_of.strip()):
                errors.append("shots[{}(id={})].continuation_of は非空文字列である必要があります".format(i, sid))
                ok_item = False

            if ok_item:
                seen_ids.add(sid)
                if scene_id is not None:
                    seen_scene_ids.add(scene_id)
                normalized_shot = {
                    "id": sid,
                    "visual_prompt": visual_prompt.strip(),
                    "motion_preset": motion_preset,
                    "duration_sec": float(duration_sec),
                    "caption_jp": caption_jp.strip(),
                }
                if scene_id is not None:
                    normalized_shot["scene_id"] = scene_id
                if narration_mode == "absent":
                    # 参考にナレーション無し → shot ナレーションも空へ正規化（codex-review High）。
                    normalized_shot["narration_jp"] = ""
                elif narration_jp is not None:
                    normalized_shot["narration_jp"] = narration_jp.strip()
                if caption_in_off is not None:
                    normalized_shot["caption_in_offset_sec"] = float(caption_in_off)
                if caption_out_off is not None:
                    normalized_shot["caption_out_offset_sec"] = float(caption_out_off)
                if telop_style_hint is not None:
                    normalized_shot["telop_style_hint"] = dict(telop_style_hint)
                if reference_visual is not None:
                    normalized_shot["reference_visual"] = dict(reference_visual)
                if normalized_captions is not None:
                    normalized_shot["captions"] = normalized_captions
                if continuation_of is not None:
                    normalized_shot["continuation_of"] = continuation_of.strip()
                normalized_shots.append(normalized_shot)

    bgm_mood = plan.get("bgm_mood")
    if bgm_mood not in BGM_MOODS:
        errors.append("bgm_mood が不正です (got: {!r})".format(bgm_mood))

    # 同一 scene_id の連続ショットは、シーンマスター1本から切り出す前提のため
    # visual_prompt が一致していなければならない（1つのシーンに複数の絵は存在しない）。
    scene_prompts = {}
    for shot in normalized_shots:
        sid = shot.get("scene_id")
        if not sid:
            continue
        vp = shot["visual_prompt"]
        prev = scene_prompts.get(sid)
        if prev is None:
            scene_prompts[sid] = vp
        elif prev != vp:
            errors.append(
                "shots の scene_id={!r} 内で visual_prompt が一致していません "
                "（同一シーンは1本のマスター動画から切り出すため、visual_promptは全メンバーで揃えてください）: "
                "{!r} vs {!r}".format(sid, prev, vp)
            )
            break

    # v2 拡張トップレベル: sfx_plan / hook_end_shot_id / cta_start_shot_id
    normalized_sfx_plan = None
    sfx_plan_raw = plan.get("sfx_plan")
    valid_shot_ids = {s["id"] for s in normalized_shots}
    shot_duration_by_id = {s["id"]: s["duration_sec"] for s in normalized_shots}
    if sfx_plan_raw is not None:
        if not isinstance(sfx_plan_raw, list):
            errors.append("sfx_plan はリストである必要があります")
        else:
            normalized_sfx_plan = []
            for i, ev in enumerate(sfx_plan_raw):
                if not isinstance(ev, dict):
                    errors.append("sfx_plan[{}] はオブジェクトではありません".format(i))
                    continue
                anc = ev.get("t_anchor")
                family = ev.get("family")
                gain_db = ev.get("gain_db")
                ok_ev = True
                if not isinstance(anc, dict):
                    errors.append("sfx_plan[{}].t_anchor はオブジェクトである必要があります".format(i))
                    ok_ev = False
                else:
                    a_type = anc.get("type")
                    a_shot_id = anc.get("shot_id")
                    a_off = anc.get("offset_sec")
                    if a_type not in _SUPPORTED_SFX_ANCHOR_TYPES:
                        errors.append(
                            "sfx_plan[{}].t_anchor.type が不正です (got: {!r}, expected one of {})".format(
                                i, a_type, list(_SUPPORTED_SFX_ANCHOR_TYPES)
                            )
                        )
                        ok_ev = False
                    if not isinstance(a_shot_id, str) or a_shot_id not in valid_shot_ids:
                        errors.append(
                            "sfx_plan[{}].t_anchor.shot_id が shots に存在しません (got: {!r})".format(i, a_shot_id)
                        )
                        ok_ev = False
                    if not _is_number(a_off):
                        errors.append("sfx_plan[{}].t_anchor.offset_sec は数値である必要があります".format(i))
                        ok_ev = False
                    elif isinstance(a_shot_id, str) and a_shot_id in shot_duration_by_id:
                        _d = shot_duration_by_id[a_shot_id]
                        if not (-1e-6 <= float(a_off) <= _d + 1e-6):
                            errors.append(
                                "sfx_plan[{}].t_anchor.offset_sec は 0〜対象shotのduration_sec の範囲である必要があります "
                                "(got: {}, shot={}, dur={})".format(i, a_off, a_shot_id, _d)
                            )
                            ok_ev = False
                if family not in _SUPPORTED_SFX_FAMILIES:
                    errors.append(
                        "sfx_plan[{}].family が不正です (got: {!r}, expected one of {})".format(
                            i, family, list(_SUPPORTED_SFX_FAMILIES)
                        )
                    )
                    ok_ev = False
                if gain_db is not None and not _is_number(gain_db):
                    errors.append("sfx_plan[{}].gain_db は数値である必要があります".format(i))
                    ok_ev = False
                if ok_ev:
                    norm_ev = {
                        "t_anchor": {
                            "type": anc["type"],
                            "shot_id": anc["shot_id"],
                            "offset_sec": float(anc["offset_sec"]),
                        },
                        "family": family,
                    }
                    if gain_db is not None:
                        norm_ev["gain_db"] = float(gain_db)
                    normalized_sfx_plan.append(norm_ev)

    # R4: continuation_of の参照整合（存在する shot id を指しているか）を後段検証。
    for shot in normalized_shots:
        cof = shot.get("continuation_of")
        if cof is not None and cof not in valid_shot_ids:
            errors.append(
                "shots[id={}].continuation_of {!r} は存在する shot id を指す必要があります".format(
                    shot.get("id"), cof,
                )
            )

    hook_end_shot_id = plan.get("hook_end_shot_id")
    cta_start_shot_id = plan.get("cta_start_shot_id")
    if hook_end_shot_id is not None:
        if not isinstance(hook_end_shot_id, str) or hook_end_shot_id not in valid_shot_ids:
            errors.append(
                "hook_end_shot_id が shots に存在しません (got: {!r})".format(hook_end_shot_id)
            )
    if cta_start_shot_id is not None:
        if not isinstance(cta_start_shot_id, str) or cta_start_shot_id not in valid_shot_ids:
            errors.append(
                "cta_start_shot_id が shots に存在しません (got: {!r})".format(cta_start_shot_id)
            )

    if errors:
        return False, errors, None

    total_duration = sum(s["duration_sec"] for s in normalized_shots)
    if target_duration_sec is not None:
        lower = max(0.0, target_duration_sec - target_tolerance_sec)
        upper = target_duration_sec + target_tolerance_sec
        if not (lower <= total_duration <= upper):
            return False, [
                "ショット尺の合計が目標から外れています: 現在約{:.1f}秒、目標は{:.0f}±{:.0f}秒"
                "（{:.0f}〜{:.0f}秒）です。shotsのduration_secを調整するか、ショット数を"
                "増減してください。".format(total_duration, target_duration_sec, target_tolerance_sec, lower, upper)
            ], None

    normalized_plan = {
        "version": version if version in _SUPPORTED_PLAN_VERSIONS else 1,
        "meta": meta,
        "concept": concept.strip(),
        "hook": hook.strip(),
        "narration_script": narration.strip(),
        "shots": normalized_shots,
        "bgm_mood": bgm_mood,
    }
    if narration_mode in ("present", "absent", "unknown"):
        normalized_plan["narration_mode"] = narration_mode
    if normalized_sfx_plan is not None:
        normalized_plan["sfx_plan"] = normalized_sfx_plan
    if hook_end_shot_id is not None:
        normalized_plan["hook_end_shot_id"] = hook_end_shot_id
    if cta_start_shot_id is not None:
        normalized_plan["cta_start_shot_id"] = cta_start_shot_id

    # TTPS/ステマ規制対応: 動画冒頭〜全編に表示する PR 表記（"#PR" 等）。
    # 任意フィールド。指定があれば非空文字列である必要がある（Noneや空は付与なしと同義）。
    pr_disclosure = plan.get("pr_disclosure")
    if pr_disclosure is not None:
        if not isinstance(pr_disclosure, str) or not pr_disclosure.strip():
            # 空・非文字列は静かに落とす（後方互換: 既存 plan が"" を渡しても壊れない）。
            pr_disclosure = None
        else:
            normalized_plan["pr_disclosure"] = pr_disclosure.strip()
    return True, [], normalized_plan


# ---------------------------------------------------------------------------
# スモーク代替plan（テスト / --no-llm 用の最小plan）
# ---------------------------------------------------------------------------
# 旧テンプレ (_default_rule_based_plan / _vertical_hook_rule_based_plan) は
# TTP v2 移行時に撤去済み。「ポイント1: 全体像を知る」等の固定文言は再導入禁止。
# ここは本番企画には使用禁止。スモーク（煙テスト）用の最小plan生成に絞る。
# ---------------------------------------------------------------------------

STYLE_NAMES = ("default", "vertical_hook")


def build_smoke_plan(theme, target_duration_sec=30, shot_count=None, style="default", aspect=None):
    """テスト/--no-llm スモーク専用の最小plan。**本番企画には使用禁止**。

    「無地背景 + テーマ文字テロップ + 目標尺を等分」の煙テスト用最小構成。
    5部構成TTP等の固定文言テンプレは持たない（TTP v2 移行時に撤去）。
    景表法NG表現（絶対稼げる/100%成功等）は一切含まない。

    Args:
        theme: 動画のテーマ文字列。
        target_duration_sec: 目標尺（秒）。
        shot_count: ショット数。None なら 3。
        style: "default" or "vertical_hook"（未知値は "default" に正規化される。
               スモーク用途では style ごとの中身は同一で、meta.style のみ記録する）。
        aspect: 任意。ログ用途のみ（生成物には影響しない）。
    """
    theme = (theme or "このテーマ").strip()
    style = style if style in STYLE_NAMES else "default"
    n = 3 if shot_count is None else max(1, int(shot_count))
    per_shot = round(float(target_duration_sec) / n, 3)
    per_shot = max(MIN_SHOT_DURATION, min(MAX_SHOT_DURATION, per_shot))

    concept = "「{}」のスモーク検証用プレースホルダplan。".format(theme)
    hook = "{}".format(theme)
    caption = theme[:MAX_CAPTION_CHARS] if theme else "テスト"
    narration_script = theme

    shots = []
    for i in range(n):
        shots.append({
            "id": "s{}".format(i + 1),
            "visual_prompt": (
                "abstract neutral background placeholder for smoke test of theme '{}'".format(theme)
            ),
            "motion_preset": "static",
            "duration_sec": per_shot,
            "caption_jp": caption,
        })
    return {
        "version": 1,
        "meta": {
            "source": "smoke",
            "model_used": None,
            "fallback_from": None,
            "fallback_reason": None,
            "attempt": 0,
            "reason": "スモーク（煙テスト）用最小plan。本番企画には使用禁止。",
            "style": style,
            "smoke": True,
        },
        "concept": concept,
        "hook": hook,
        "narration_script": narration_script,
        "shots": shots,
        "bgm_mood": "upbeat",
    }


def build_rule_based_plan(theme, target_duration_sec=30, shot_count=None, style="default"):
    """後方互換の薄いエイリアス。実体は build_smoke_plan。

    旧テンプレ (「ポイント1: 全体像を知る」等) は TTP v2 移行時に撤去した。
    本番企画には使わないこと（テスト/スモーク専用）。source は "rule" として
    返すことで、jobs.py 側の meta 参照や既存テスト（メタが "rule" を期待する
    ものが T9〜T11 の書換え途上で残る場合）と衝突しないようにする。
    """
    plan = build_smoke_plan(
        theme, target_duration_sec=target_duration_sec, shot_count=shot_count, style=style
    )
    # 旧呼び出し側との整合のため "source" のみ "rule" に写像する（中身はスモーク）。
    plan["meta"]["source"] = "rule"
    plan["meta"]["reason"] = "--no-llm指定 or 旧呼び出し互換のためスモーク代替plan"
    return plan
