# -*- coding: utf-8 -*-
"""プラン階層（むりょうコース / 本番コース）を1変数 plan_tier に集約するための単一の真実源。

発注意図（2026-07-25 ユーザー要望）:
  「無料 or 有料のワンセット」。動画生成だけ無料で他は有料…という混在をやめ、
  1つのプラン選択で 映像・声・文字起こし・BGM/SE の課金/非課金をまとめて切り替える。

対応表:
                free（むりょう・おためし）        paid（本番・有料）
  映像(backend)  mock（練習用の仮映像・0円）        higgsfield（実映像・クレジット消費）
  声(tts)        macOS say（無料）                  Fish Audio（有料）
  文字起こし(asr) スキップ（外部API呼ばない）         Fish Audio ASR（有料）
  BGM/SE         自前ライブラリ（同一・無料）        自前ライブラリ（同一）

★0円保証: plan_tier="free" のとき、Higgsfield / Fish Audio の外部（課金）API 呼び出しの
  コードパスへ一切入らないことを機械検証する（tests/test_free_zero_cost_guarantee.py）。
  この保証は本モジュールの resolve_backend() と apply_tier_to_cfg() の2点に集約されている。

backend/voice/asr の個別指定は UI からは撤去し、config/CLI（上級者向け）にのみ残す（後方互換）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import copy
from typing import Optional

FREE = "free"
PAID = "paid"
VALID_TIERS = (FREE, PAID)

# 各プランの映像バックエンド（plan_tier は backend より優先される）。
_TIER_BACKEND = {FREE: "mock", PAID: "higgsfield"}
# 各プランの TTS エンジン（pipeline.tts.get_tts_backend が cfg["tts"]["engine"] を見る）。
_TIER_TTS_ENGINE = {FREE: "say", PAID: "fish_audio"}


def normalize_tier(plan_tier) -> Optional[str]:
    """plan_tier を FREE/PAID に正規化する。無効・未指定は None を返す。"""
    if isinstance(plan_tier, str):
        v = plan_tier.strip().lower()
        if v in VALID_TIERS:
            return v
    return None


def infer_tier(plan_tier=None, backend_name=None) -> str:
    """有効な plan_tier があればそれを、無ければ backend 名から後方互換で推定する。

    既存プロジェクト（plan_tier 未保存）や上級者の --backend 指定を、表示・課金判定のために
    2択へマッピングする: backend=="mock" → free / それ以外（higgsfield/cloudapi）→ paid。
    """
    tier = normalize_tier(plan_tier)
    if tier is not None:
        return tier
    return FREE if (backend_name or "mock") == "mock" else PAID


def resolve_backend(plan_tier=None, requested_backend=None) -> str:
    """映像バックエンド名を決める。plan_tier が有効なら backend 個別指定より優先する。

    - plan_tier="free" → 常に "mock"（Higgsfield を構築させない＝0円保証の要）。
    - plan_tier="paid" → 常に "higgsfield"。
    - plan_tier 未指定 → requested_backend（後方互換）。無ければ "mock"。
    """
    tier = normalize_tier(plan_tier)
    if tier is not None:
        return _TIER_BACKEND[tier]
    return requested_backend or "mock"


def apply_tier_to_cfg(cfg, plan_tier):
    """plan_tier に応じて cfg の TTS エンジン・ASR 有効/無効を上書きした「新しい dict」を返す。

    元の cfg は破壊しない（deepcopy）。plan_tier 未指定/無効なら cfg をそのまま複製して返す
    （後方互換: config/CLI の個別指定を尊重する）。

    free のとき:
      - cfg["tts"]["engine"] = "say"       → Fish Audio TTS を呼ばない
      - cfg["reference"]["asr_enabled"] = False → Fish Audio ASR を呼ばない
    paid のとき:
      - cfg["tts"]["engine"] = "fish_audio"
      - cfg["reference"]["asr_enabled"] = True
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    tier = normalize_tier(plan_tier)
    if tier is None:
        return out
    tts = out.setdefault("tts", {})
    if not isinstance(tts, dict):
        tts = {}
        out["tts"] = tts
    tts["engine"] = _TIER_TTS_ENGINE[tier]
    ref = out.setdefault("reference", {})
    if not isinstance(ref, dict):
        ref = {}
        out["reference"] = ref
    ref["asr_enabled"] = (tier == PAID)
    return out


# 見積の概算パラメータ（実 Higgsfield CLI（`generate cost`）が使えない環境向けの縮退見積）。
# 実測に基づく厳密値ではなく「事前に桁感を伝える」ための概算であることを approximate=True で明示する。
_EST_AVG_SHOT_SEC = 4.0   # 1ショットの平均尺（概算）
_EST_MIN_SHOTS = 3
_EST_MAX_SHOTS = 14


def estimate_shot_count(duration_sec) -> int:
    """尺（秒）からショット数を概算する（見積用）。"""
    try:
        d = float(duration_sec)
    except (TypeError, ValueError):
        d = 30.0
    if d <= 0:
        d = 30.0
    n = int(round(d / _EST_AVG_SHOT_SEC))
    return max(_EST_MIN_SHOTS, min(_EST_MAX_SHOTS, n))


def estimate_coins(cfg, plan_tier, duration_sec=30):
    """開始前に表示する費用見積を返す（実生成はしない・クレジットは消費しない）。

    free は常に 0 コイン（0円保証）。paid は config の higgsfield.max_credits_per_shot を
    1ショットあたりの上限クレジットとして、ショット数概算 × 単価 で合計コインを見積もる。

    Returns:
        {"plan_tier": str, "coins": int, "shot_count": int, "per_shot": int,
         "approximate": bool, "note": str}
    """
    tier = infer_tier(plan_tier)
    if tier == FREE:
        return {
            "plan_tier": FREE, "coins": 0, "shot_count": 0, "per_shot": 0,
            "approximate": False, "note": "むりょうコースは0円です（外部の有料APIを一切使いません）",
        }
    hf = (cfg or {}).get("higgsfield") or {}
    per_shot = hf.get("max_credits_per_shot", 10)
    try:
        per_shot = int(per_shot)
    except (TypeError, ValueError):
        per_shot = 10
    shot_count = estimate_shot_count(duration_sec)
    coins = shot_count * per_shot
    return {
        "plan_tier": PAID, "coins": coins, "shot_count": shot_count, "per_shot": per_shot,
        "approximate": True,
        "note": "約{}コイン消費します（{}ショット×最大{}コインの概算）".format(coins, shot_count, per_shot),
    }
