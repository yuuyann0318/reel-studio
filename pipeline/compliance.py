# -*- coding: utf-8 -*-
"""台本・キャプション文字列のコンプライアンス検査。

CLAUDE.md「禁止事項（ONE運営）」準拠:
- 景品表示法NG表現（「絶対稼げる」「100%成功」等）を禁止
- 競合名義（「みお」「@mio_ai_insta_」）を成果物へ一切登場させない
- 特定商取引法の観点（解約条件の不明示等）は注意フラグとして検出する

違反があれば run を止め、理由を report に記録できるよう check_plan() が
{"ok": bool, "violations": [...], "warnings": [...]} を返す。例外は投げない。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

DEFAULT_NG_WORDS = [
    "絶対稼げる",
    "絶対に稼げる",
    "100%成功",
    "100%儲かる",
    "元本保証",
    "必ず儲かる",
    "誰でも稼げる",
    "みお",
    "@mio_ai_insta_",
]

# 特定商取引法の観点で「言及している場合は解約条件も明示すべき」注意ワード
# （出現したら警告のみ・runは止めない）
TOKUSHOHO_WATCH_WORDS = ["月額", "サブスク", "定期購入", "自動更新", "契約"]
TOKUSHOHO_REQUIRED_HINTS = ["解約", "退会", "いつでもやめられる", "解除"]


def _collect_texts(plan):
    """plan中の検査対象テキスト一覧を返す（フィールド名付き）。"""
    plan = plan or {}
    texts = []
    if plan.get("concept"):
        texts.append(("concept", plan["concept"]))
    if plan.get("hook"):
        texts.append(("hook", plan["hook"]))
    if plan.get("narration_script"):
        texts.append(("narration_script", plan["narration_script"]))
    for shot in plan.get("shots", []) or []:
        sid = shot.get("id", "?")
        if shot.get("caption_jp"):
            texts.append(("shots[{}].caption_jp".format(sid), shot["caption_jp"]))
        if shot.get("visual_prompt"):
            texts.append(("shots[{}].visual_prompt".format(sid), shot["visual_prompt"]))
    return texts


def check_plan(plan, ng_words=None):
    """plan全体をNGワードで検査する。

    Returns: {"ok": bool, "violations": [{"field","word","text"}...],
              "warnings": [{"field","reason"}...]}
    ok=False の場合、呼び出し側(run.py)はレンダリングへ進んではならない。
    """
    ng_words = ng_words if ng_words is not None else DEFAULT_NG_WORDS
    texts = _collect_texts(plan)

    violations = []
    for field, text in texts:
        for word in ng_words:
            if word and word in text:
                violations.append({"field": field, "word": word, "text": text})

    warnings = []
    full_text = "".join(t for _, t in texts)
    if any(w in full_text for w in TOKUSHOHO_WATCH_WORDS) and not any(
        h in full_text for h in TOKUSHOHO_REQUIRED_HINTS
    ):
        warnings.append(
            {
                "field": "narration_script/caption",
                "reason": "月額/サブスク等の継続課金を示唆する語がありますが、解約条件に触れる語が"
                "見当たりません。特定商取引法の観点で解約条件の明示を検討してください。",
            }
        )

    return {"ok": len(violations) == 0, "violations": violations, "warnings": warnings}
