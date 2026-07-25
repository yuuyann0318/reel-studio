# -*- coding: utf-8 -*-
"""台本・キャプション文字列のコンプライアンス検査。

運用ルール準拠:
- 景品表示法NG表現（「絶対稼げる」「100%成功」等）を禁止
- 特定の個人名・競合アカウント名を成果物へ一切登場させない
  （具体的な実名リストは公開対象外の `config.local/ngwords.json` に別出しし、
   ローカル環境ごとに管理する。ファイルが無ければ空リストで動作。）
- 特定商取引法の観点（解約条件の不明示等）は注意フラグとして検出する

違反があれば run を止め、理由を report に記録できるよう check_plan() が
{"ok": bool, "violations": [...], "warnings": [...]} を返す。例外は投げない。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# 公開対象に含めない実名NGワード辞書のパス（プロジェクト直下 `config.local/ngwords.json`）。
# フォーマット: {"ng_words": ["...", ...]} または ["...", ...]（後者は後方互換）。
# 無ければ空リスト扱い＝tracked コードだけで動作する。
_LOCAL_NG_WORDS_PATH = Path(__file__).resolve().parents[1] / "config.local" / "ngwords.json"

DEFAULT_NG_WORDS = [
    "絶対稼げる",
    "絶対に稼げる",
    "100%成功",
    "100%儲かる",
    "元本保証",
    "必ず儲かる",
    "誰でも稼げる",
]


def _load_local_ng_words():
    """`config.local/ngwords.json` から追加NGワードを読み込む。無ければ空リスト。

    tracked コード側には特定の実名は残さず、ローカル辞書で維持する運用のため。
    JSON破損や読み取り失敗時は空リスト（例外は投げず、既定挙動を維持）。
    """
    path = _LOCAL_NG_WORDS_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        words = data.get("ng_words") or []
    elif isinstance(data, list):
        words = data
    else:
        return []
    return [w for w in words if isinstance(w, str) and w]


def get_effective_ng_words():
    """DEFAULT_NG_WORDS にローカル辞書の追加分をマージして返す。"""
    merged = list(DEFAULT_NG_WORDS)
    for w in _load_local_ng_words():
        if w not in merged:
            merged.append(w)
    return merged

# 特定商取引法の観点で「言及している場合は解約条件も明示すべき」注意ワード
# （出現したら警告のみ・runは止めない）
TOKUSHOHO_WATCH_WORDS = ["月額", "サブスク", "定期購入", "自動更新", "契約"]
TOKUSHOHO_REQUIRED_HINTS = ["解約", "退会", "いつでもやめられる", "解除"]

# 美容系商品アフィリ動画モード向け薬機法(医薬品医療機器等法)NGワード。
# DEFAULT_NG_WORDSには混ぜない（商品モードでcheck_plan()呼び出し側が明示的に
# ng_words/ng_patternsへ合成して渡す設計。既定モードの挙動は不変にするため）。
BEAUTY_YAKKIHO_NG_WORDS = [
    "シミが消える",
    "シワが消える",
    "必ず痩せる",
    "絶対痩せる",
    "飲むだけで痩せる",
    "塗るだけで痩せる",
    "若返る",
    "アトピーが治る",
    "ニキビが治る",
    "毛穴が消える",
    "白髪がなくなる",
    "医学的に効果が証明",
]

# 「シミ/シワ/ニキビ/たるみ/くすみ」等が「消える/なくなる/治る」と断定する表現を
# 広く検出する正規表現。BEAUTY_YAKKIHO_NG_WORDSの固定文言だけでは拾えない言い回し
# （例:「頑固なシミがみるみる消えます」）を補足する。
BEAUTY_YAKKIHO_NG_PATTERNS = [
    r"(シミ|シワ|ニキビ|たるみ|くすみ|毛穴)が(すぐに|みるみる|完全に)?(消え|なくな|治)",
    r"(必ず|絶対|100%)(痩せ|効く|治る|若返る)",
]

# 景表法(優良誤認・誇大広告)の断定・最上級ワード。
# TTPS 台本モード（美容アフィリ）で ng_words へ合成して使う。DEFAULT_NG_WORDS には
# 混ぜない（既定モードの挙動を不変に保つ設計方針を BEAUTY_YAKKIHO_NG_WORDS と揃える）。
# 単独語では誤爆しやすいため、断定を強める文脈語との複合形で持つ。
BEAUTY_KEIHYO_NG_WORDS = [
    "絶対に効く",
    "絶対効く",
    "必ず効く",
    "必ず結果が出る",
    "100%効く",
    "100％効く",
    "誰でも痩せる",
    "誰でも綺麗になる",
    "即効で",
    "永久に",
    "業界No.1",
    "業界最高",
]

# 景表法の断定・最上級を広く拾う正規表現。
# 「必ず」「絶対」「100%」等の単独出現も検査に含める（美容アフィリではこれらの
# 単独語がそのまま断定効果訴求として機能してしまうため）。
BEAUTY_KEIHYO_NG_PATTERNS = [
    r"(必ず|絶対|100[%％])(効く|治る|痩せ|若返|消え|なくな|きれい|綺麗)",
    r"(即効|即効性|永久に|一瞬で)(効く|治る|痩せ|美白|美肌|若返)",
    r"業界(No\.?\s*1|最高|最強|唯一)",
]

# 「効果に触れているshot」を検知するためのパターン。
# ヒットしたshotには「※効果には個人差があります」等の注記を自動付与すべき、と
# 判断する材料になる（判定は detect_effect_mention_shot_ids() で行う）。
EFFECT_MENTION_PATTERNS = [
    r"(効果|効いた|効き|変化|結果|実感|改善|マシに|楽に|軽く)",
    r"[0-9０-９]+(日|日目|日間|週間|ヶ月|か月)で",
    r"(痩せ|美白|美肌|ハリ|ツヤ|うるおい|保湿|引き締|くすみ抜け)",
    r"(ビフォー|アフター|before|after)",
]

# TTPS モードで注記に使う定型文（テロップ / ナレーション両方に流用）。
INDIVIDUAL_VARIANCE_NOTE = "※効果には個人差があります"
PR_DISCLOSURE_DEFAULT = "#PR"


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
        # narration_jp（音声主導タイミング同期モード用のショット別ナレーション断片）は
        # narration_script（全文）とは別にTTS実音声へそのまま渡る文言のため、
        # ここで検査対象に加えないとcheck_plan()をすり抜けてNGワードが音声化されてしまう
        # （narration_scriptだけ検査していた従来の検査漏れの修正）。
        if shot.get("narration_jp"):
            texts.append(("shots[{}].narration_jp".format(sid), shot["narration_jp"]))
    return texts


def check_plan(plan, ng_words=None, ng_patterns=None):
    """plan全体をNGワード(+任意でNGパターン)で検査する。

    Returns: {"ok": bool, "violations": [{"field","word","text"}...],
              "warnings": [{"field","reason"}...]}
    ok=False の場合、呼び出し側(run.py)はレンダリングへ進んではならない。

    ng_patterns: 正規表現文字列(またはコンパイル済みPatternオブジェクト)のリスト。
    Noneの場合はパターン検査を一切行わず、既存のng_wordsのみの挙動と完全に不変
    （後方互換）。マッチした場合は violations に {"field","word","text","pattern"}
    を追加する（"word"にはマッチした実際の部分文字列を入れる）。
    """
    ng_words = ng_words if ng_words is not None else get_effective_ng_words()
    texts = _collect_texts(plan)

    violations = []
    for field, text in texts:
        for word in ng_words:
            if word and word in text:
                violations.append({"field": field, "word": word, "text": text})
        if ng_patterns:
            for pat in ng_patterns:
                compiled = pat if hasattr(pat, "search") else re.compile(pat)
                m = compiled.search(text)
                if m:
                    violations.append(
                        {"field": field, "word": m.group(0), "text": text, "pattern": compiled.pattern}
                    )

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


# ---------------------------------------------------------------------------
# TTPS モード（美容アフィリ台本）向けヘルパー
# ---------------------------------------------------------------------------

def build_ttps_ng_words(base_ng_words=None):
    """TTPS モード用に、既定NG語 + 薬機法NG語 + 景表法NG語をマージした語リストを返す。

    base_ng_words を渡した場合はそれをベースにする（呼び出し側が resolve_ng_words()
    で得た「config.local + ローカル辞書」も含めた集合をそのまま拡張したい場面向け）。
    None の場合は get_effective_ng_words() を起点にする（DEFAULT_NG_WORDS + ローカル辞書）。
    重複は取り除く（順序は先勝ちで保持）。
    """
    base = list(base_ng_words) if base_ng_words is not None else list(get_effective_ng_words())
    merged = list(base)
    for w in list(BEAUTY_YAKKIHO_NG_WORDS) + list(BEAUTY_KEIHYO_NG_WORDS):
        if w and w not in merged:
            merged.append(w)
    return merged


def build_ttps_ng_patterns():
    """TTPS モード用の正規表現パターン列を返す。

    美容薬機法 + 景表法 の両パターンをまとめる。呼び出し側は check_plan() の
    ng_patterns 引数へそのまま渡す。
    """
    return list(BEAUTY_YAKKIHO_NG_PATTERNS) + list(BEAUTY_KEIHYO_NG_PATTERNS)


def detect_effect_mention_shot_ids(plan):
    """plan.shots のうち「効果・変化・数字・ビフォーアフター」に触れているshot idを返す。

    caption_jp / narration_jp / captions[].text を対象に EFFECT_MENTION_PATTERNS で
    走査する。ヒットしたshotには pipeline 側で「※効果には個人差があります」テロップを
    自動付与するための材料になる（このモジュールは検知のみ・付与は subtitles / run 側）。

    Returns: List[str] — 検知した shot id のリスト（plan.shots 順・重複除去）。
    """
    if not isinstance(plan, dict):
        return []
    shots = plan.get("shots") or []
    compiled = [re.compile(p) for p in EFFECT_MENTION_PATTERNS]
    hit_ids = []
    seen = set()
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        sid = shot.get("id")
        if not sid or sid in seen:
            continue
        texts = []
        if isinstance(shot.get("caption_jp"), str):
            texts.append(shot["caption_jp"])
        if isinstance(shot.get("narration_jp"), str):
            texts.append(shot["narration_jp"])
        for cap in shot.get("captions") or []:
            if isinstance(cap, dict):
                for k in ("text", "caption_jp"):
                    v = cap.get(k)
                    if isinstance(v, str):
                        texts.append(v)
        joined = "\n".join(texts)
        if not joined:
            continue
        for pat in compiled:
            if pat.search(joined):
                hit_ids.append(sid)
                seen.add(sid)
                break
    return hit_ids


def is_ttps_mode(cfg, plan=None, product=None):
    """設定・plan・productから、TTPS美容アフィリ台本モードとして扱うべきかを判定する。

    優先順位:
      1. cfg.ttps.enabled が明示的に True/False なら従う。
      2. product 情報（{"name":..., "url":...} 等）が非空なら True。
      3. plan.meta.product が非空文字列なら True。
      4. それ以外は False。

    呼び出し側は director / run から判定して、compliance の NG語辞書切り替えや
    TTPS プロンプト注入・ #PR テロップ自動付与の on/off を決める。
    """
    ttps_cfg = (cfg or {}).get("ttps") if isinstance(cfg, dict) else None
    if isinstance(ttps_cfg, dict) and ttps_cfg.get("enabled") is not None:
        return bool(ttps_cfg["enabled"])
    if product:
        # dict でも str でも「非空」なら product 指定ありとみなす
        if isinstance(product, dict) and any(product.values()):
            return True
        if isinstance(product, str) and product.strip():
            return True
    if isinstance(plan, dict):
        meta = plan.get("meta") or {}
        p = meta.get("product")
        if isinstance(p, str) and p.strip():
            return True
    return False
