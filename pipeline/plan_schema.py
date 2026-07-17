# -*- coding: utf-8 -*-
"""reel_plan（テーマ→9:16リール企画）のバリデーション・正規化・ルールベース代替生成。

video-auto-editor/pipeline/plan_schema.py のパターン（validate_plan が (ok, errors,
normalized_plan) を返す・エラーは矯正リトライへ差し戻す・claude全滅時のルールベース
代替を必ず用意する）を踏襲しつつ、対象が「編集判断(keep_segments)」ではなく
「ゼロから作る企画(コンセプト/フック/台本/ショットリスト)」である点に合わせて
スキーマを作り直した。

plan スキーマ:
{
  "version": 1,
  "meta": {"source": "ai"|"rule", "model_used": str|None, "fallback_from": str|None,
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
    }, ...
  ],
  "bgm_mood": "upbeat"|"calm"|"emotional"|"none",
}

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

MOTION_PRESETS = ("static", "pan_left", "pan_right", "zoom_in", "zoom_out", "ken_burns")
BGM_MOODS = ("upbeat", "calm", "emotional", "none")

MIN_SHOT_DURATION = 1.0
MAX_SHOT_DURATION = 20.0
MAX_CAPTION_CHARS = 40


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

    if plan.get("version") != 1:
        errors.append("version は 1 固定である必要があります (got: {!r})".format(plan.get("version")))

    meta = plan.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta が存在しないかオブジェクトではありません")
        meta = {}
    elif meta.get("source") not in ("ai", "rule"):
        errors.append("meta.source は 'ai' か 'rule' である必要があります (got: {!r})".format(meta.get("source")))

    concept = plan.get("concept")
    if not isinstance(concept, str) or not concept.strip():
        errors.append("concept は非空文字列である必要があります")
        concept = ""

    hook = plan.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        errors.append("hook は非空文字列である必要があります")
        hook = ""

    narration = plan.get("narration_script")
    if not isinstance(narration, str) or not narration.strip():
        errors.append("narration_script は非空文字列である必要があります")
        narration = ""

    shots_raw = plan.get("shots")
    normalized_shots = []
    if not isinstance(shots_raw, list) or not shots_raw:
        errors.append("shots は非空リストである必要があります")
    else:
        seen_ids = set()
        for i, shot in enumerate(shots_raw):
            if not isinstance(shot, dict):
                errors.append("shots[{}] はオブジェクトではありません".format(i))
                continue
            sid = shot.get("id") or "s{}".format(i + 1)
            if sid in seen_ids:
                errors.append("shots の id が重複しています: {!r}".format(sid))
                continue
            visual_prompt = shot.get("visual_prompt")
            motion_preset = shot.get("motion_preset", "static")
            duration_sec = shot.get("duration_sec")
            caption_jp = shot.get("caption_jp", "")

            ok_item = True
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

            if ok_item:
                seen_ids.add(sid)
                normalized_shots.append(
                    {
                        "id": sid,
                        "visual_prompt": visual_prompt.strip(),
                        "motion_preset": motion_preset,
                        "duration_sec": float(duration_sec),
                        "caption_jp": caption_jp.strip(),
                    }
                )

    bgm_mood = plan.get("bgm_mood")
    if bgm_mood not in BGM_MOODS:
        errors.append("bgm_mood が不正です (got: {!r})".format(bgm_mood))

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
        "version": 1,
        "meta": meta,
        "concept": concept.strip(),
        "hook": hook.strip(),
        "narration_script": narration.strip(),
        "shots": normalized_shots,
        "bgm_mood": bgm_mood,
    }
    return True, [], normalized_plan


# ---------------------------------------------------------------------------
# ルールベース代替plan（claude 全滅時 / --no-llm 指定時のフォールバック）
# ---------------------------------------------------------------------------

_NG_PREFIXES_FOR_TEMPLATE = ("絶対稼げる", "100%成功")  # compliance.py のNGワードとは独立に、そもそもテンプレへ混入させない


STYLE_NAMES = ("default", "vertical_hook")

# vertical_hookは縦書きテロップで映えるよう短いcaptionを使う（8〜14字目安）。
# テーマ文字列そのものは任意長のため、テンプレへ埋め込む際はここまでに切り詰める。
_VERTICAL_HOOK_THEME_TRUNCATE = 14


def _default_rule_based_plan(theme, target_duration_sec, shot_count):
    shot_count = max(3, int(shot_count or 5))
    per_shot = round(float(target_duration_sec) / shot_count, 2)
    per_shot = max(MIN_SHOT_DURATION, min(MAX_SHOT_DURATION, per_shot))

    concept = "「{}」を、初心者にもわかる3つのポイントで紹介するショート動画。".format(theme)
    hook = "{}、実は今日から始められます。".format(theme)
    narration_parts = [
        "{}について、今日は3つのポイントに絞って紹介します。".format(theme),
        "1つ目は、まず全体像を知ること。何をどう始めればいいか、最初の一歩を明確にします。",
        "2つ目は、小さく試してみること。完璧を目指さず、まず手を動かすことが上達の近道です。",
        "3つ目は、続ける仕組みを作ること。毎日少しずつでも積み重ねれば、必ず形になります。",
        "気になった方は、ぜひ保存していつでも見返してくださいね。",
    ]
    narration_script = "".join(narration_parts)

    caption_templates = [
        "{}を始めよう".format(theme)[:MAX_CAPTION_CHARS],
        "ポイント1: 全体像を知る",
        "ポイント2: 小さく試す",
        "ポイント3: 続ける仕組み",
        "今日から一歩踏み出そう",
    ]
    motion_cycle = ["zoom_in", "pan_right", "pan_left", "ken_burns", "zoom_out"]
    visual_templates = [
        "abstract geometric background representing the theme '{}', soft gradient, clean minimal style".format(theme),
        "abstract icon representing step one, simple flat design, soft gradient background",
        "abstract icon representing step two, simple flat design, soft gradient background",
        "abstract icon representing step three, simple flat design, soft gradient background",
        "abstract uplifting geometric background, warm gradient, clean minimal style",
    ]

    shots = []
    for i in range(shot_count):
        idx = i % len(caption_templates)
        shots.append(
            {
                "id": "s{}".format(i + 1),
                "visual_prompt": visual_templates[idx % len(visual_templates)],
                "motion_preset": motion_cycle[i % len(motion_cycle)],
                "duration_sec": per_shot,
                "caption_jp": caption_templates[idx],
            }
        )
    return concept, hook, narration_script, shots


def _vertical_hook_caption_for_index(i, shot_count, theme_short):
    """5部構成TTP（フック/自分ごと化/本題/注意喚起/締め）に沿ったcaptionを組み立てる。"""
    main_points = ["やることは3つだけ", "最初の一歩がカギ", "コツはシンプル", "続けるほど伸びる", "ポイントを整理しよう"]
    if i == 0:
        if theme_short.startswith("今話題の"):
            return theme_short[:MAX_CAPTION_CHARS]
        return "今話題の{}".format(theme_short)[:MAX_CAPTION_CHARS]
    if i == shot_count - 1:
        return "続きは保存して見返してね"
    if shot_count >= 4 and i == 1:
        return "悔しくて調べまくった結果"
    if shot_count >= 5 and i == shot_count - 2:
        return "実はここに落とし穴が"
    main_start = 2 if shot_count >= 4 else 1
    return main_points[(i - main_start) % len(main_points)]


def _vertical_hook_rule_based_plan(theme, target_duration_sec, shot_count):
    # 高速カット構成: shots数の目安は尺÷2秒（下限3）。
    if shot_count is None:
        shot_count = max(3, int(round(float(target_duration_sec) / 2.0)))
    shot_count = max(3, int(shot_count))
    per_shot = round(float(target_duration_sec) / shot_count, 2)
    per_shot = max(1.5, min(2.5, per_shot))  # vertical_hookは1.5〜2.5秒/ショットの高速カット目安

    theme_short = theme[:_VERTICAL_HOOK_THEME_TRUNCATE]
    hook_subject = theme_short if theme_short.startswith("今話題の") else "今話題の{}".format(theme_short)
    concept = "「{}」を、今話題の切り口×自分ごと化のフックでテンポよく紹介する縦型ショート動画。".format(theme)
    hook = "{}、実は今日から始められます。".format(hook_subject)
    narration_script = (
        "{}が今すごく話題になっていて、正直悔しくて調べまくりました。".format(theme)
        + "結果からいうと、やることはシンプルで、最初の一歩さえ間違えなければ誰でも進められます。"
        + "ただ、意外な落とし穴もあるので注意してください。"
        + "気になった方は、保存していつでも見返してくださいね。"
    )

    motion_cycle = ["zoom_in", "pan_right", "zoom_out", "pan_left", "ken_burns"]
    visual_templates = [
        "abstract geometric background representing the theme '{}', soft gradient, clean minimal style, fast-paced".format(theme),
        "abstract icon representing a relatable realization moment, simple flat design, soft gradient background",
        "abstract icon representing a key point, simple flat design, soft gradient background",
        "abstract icon representing a caution or surprising fact, simple flat design, soft gradient background",
        "abstract uplifting geometric background, warm gradient, clean minimal style",
    ]

    shots = []
    for i in range(shot_count):
        shots.append(
            {
                "id": "s{}".format(i + 1),
                "visual_prompt": visual_templates[i % len(visual_templates)],
                "motion_preset": motion_cycle[i % len(motion_cycle)],
                "duration_sec": per_shot,
                "caption_jp": _vertical_hook_caption_for_index(i, shot_count, theme_short),
            }
        )
    return concept, hook, narration_script, shots


def build_rule_based_plan(theme, target_duration_sec=30, shot_count=None, style="default"):
    """テンプレートベースの決定論的フォールバックplan。claude CLI不通時 / --no-llm 指定時に使う。

    景表法NG表現（絶対稼げる/100%成功等）は一切使わないテンプレート文言のみで構成する。
    style: "default"（従来のテンプレート、shot_count省略時は5） | "vertical_hook"
    （縦書きテロップ・高速カット向けの5部構成TTPテンプレート、shot_count省略時は尺÷2秒目安）。
    """
    theme = (theme or "このテーマ").strip()
    style = style if style in STYLE_NAMES else "default"

    if style == "vertical_hook":
        concept, hook, narration_script, shots = _vertical_hook_rule_based_plan(theme, target_duration_sec, shot_count)
        reason = "--no-llm指定 または claude CLI不通のためテンプレートベース生成(vertical_hook)"
    else:
        concept, hook, narration_script, shots = _default_rule_based_plan(theme, target_duration_sec, shot_count)
        reason = "--no-llm指定 または claude CLI不通のためテンプレートベース生成"

    return {
        "version": 1,
        "meta": {
            "source": "rule",
            "model_used": None,
            "fallback_from": None,
            "fallback_reason": None,
            "attempt": 0,
            "reason": reason,
            "style": style,
        },
        "concept": concept,
        "hook": hook,
        "narration_script": narration_script,
        "shots": shots,
        "bgm_mood": "upbeat",
    }
