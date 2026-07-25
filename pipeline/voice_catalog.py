# -*- coding: utf-8 -*-
"""ナレーション音声（声）カタログと自動選択。

「音声もTTP（参考に寄せる）したい・声を選べたら最高」というユーザー要望に応える中核。

2 tier:
  - free : macOS `say -v ?` から実機列挙した日本語対応の声（Kyoko / Otoya など）。課金ゼロ。
  - paid : Fish Audio に登録された声（config.voices.fish の登録制）。参考 reference_id を切替えられる。

カタログスキーマ（各エントリ）:
  {"key": str, "tier": "free"|"paid", "label": str(平易な日本語),
   "gender": "male"|"female"|"unknown", "engine": "say"|"fish",
   "engine_voice_id": str, "pitch": "low"|"mid"|"high"|None(任意)}

自動選択（voice_mode="auto"）:
  参考動画の話者推定 spec.narrator_voice（gender_guess / pitch）に最も近い声を、
  現在の tier（free/paid）の中から gender→pitch の優先で選ぶ。無ければ tier 既定へフォールバック。

Python 3.9 互換構文のみ。外部依存ゼロ（subprocess の `say` のみ）。
"""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional


# `say` の日本語ボイス名 → 性別ヒント（macOS の既定/ノベルティ声。実機に存在するものだけ列挙採用）。
# 不明なものは "unknown"（自動選択では gender 一致に使わない）。
_SAY_GENDER_HINTS = {
    "Kyoko": "female",
    "Otoya": "male",
    "Grandma": "female",
    "Grandpa": "male",
    "Sandy": "female",
    "Shelley": "female",
    "Flo": "female",
    "Eddy": "male",
    "Reed": "male",
    "Rocko": "male",
}

# 代表的な声の pitch ヒント（自動選択の第2キー。無い声は None）。
_SAY_PITCH_HINTS = {
    "Kyoko": "mid",
    "Otoya": "low",
    "Grandma": "mid",
    "Grandpa": "low",
}

# 平易な日本語ラベル（高校生でも分かる語彙で。無い声はエンジン名から生成）。
_SAY_LABELS = {
    "Kyoko": "落ち着いた女性（定番）",
    "Otoya": "落ち着いた男性（定番）",
    "Grandma": "やさしい年配女性",
    "Grandpa": "やさしい年配男性",
    "Sandy": "明るい女性",
    "Shelley": "やわらかい女性",
    "Flo": "元気な女性",
    "Eddy": "落ち着いた男性",
    "Reed": "はっきりした男性",
    "Rocko": "力強い男性",
}

# free tier の既定ボイス名（実機に存在すればこれを既定に据える）。
_FREE_DEFAULT_VOICE = "Kyoko"

# 定番の高品質ボイス（自動選択のタイ時に優先。ノベルティ声より自然な日本語）。
_SAY_PREFERRED = {"Kyoko", "Otoya"}


def _gender_label(gender: str) -> str:
    return {"female": "女性", "male": "男性"}.get(gender, "声")


def list_say_japanese_voices(_run=None) -> List[Dict[str, Any]]:
    """macOS `say -v ?` から日本語対応（ja_JP）の声を実機列挙する。

    Returns: [{"name": str, "gender": str, "pitch": str|None, "label": str}, ...]
    `say` が無い / 失敗した環境では空リストを返す（呼び出し側で fallback すること）。
    _run: テスト差し替え用（(cmd)->CompletedProcess 風。stdout: bytes）。
    """
    runner = _run or (lambda cmd: subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
    ))
    try:
        proc = runner(["say", "-v", "?"])
    except Exception:
        return []
    stdout = getattr(proc, "stdout", b"") or b""
    if isinstance(stdout, bytes):
        listing = stdout.decode("utf-8", "replace")
    else:
        listing = str(stdout)
    voices: List[Dict[str, Any]] = []
    seen = set()
    for line in listing.splitlines():
        if not line.strip():
            continue
        # 形式: "Kyoko               ja_JP    # ..." もしくは
        #       "Eddy (日本語（日本）)      ja_JP    # ..."
        if "ja_JP" not in line:
            continue
        name = line.split()[0]
        if not name or name in seen:
            continue
        seen.add(name)
        gender = _SAY_GENDER_HINTS.get(name, "unknown")
        label = _SAY_LABELS.get(name) or "{}（{}）".format(name, _gender_label(gender))
        voices.append({
            "name": name,
            "gender": gender,
            "pitch": _SAY_PITCH_HINTS.get(name),
            "label": label,
        })
    return voices


def _fish_voices_from_cfg(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """config.voices.fish の登録制リストから paid tier のエントリを組み立てる。

    各要素は {"id": str, "label": str?, "gender": str?, "style": str?, "pitch": str?}。
    id が無い要素はスキップ。実在しない声 ID をここで捏造しないこと（登録は運用者が行う）。
    """
    voices_cfg = ((cfg or {}).get("voices") or {}).get("fish") or []
    out: List[Dict[str, Any]] = []
    seen = set()
    for v in voices_cfg:
        if not isinstance(v, dict):
            continue
        vid = v.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({
            "id": str(vid),
            "label": v.get("label") or "Fish声 {}".format(str(vid)[:6]),
            "gender": v.get("gender") or "unknown",
            "style": v.get("style"),
            "pitch": v.get("pitch"),
        })
    return out


def build_catalog(cfg: Optional[Dict[str, Any]] = None, _say_run=None) -> List[Dict[str, Any]]:
    """free（say 実機列挙）+ paid（config.voices.fish 登録制）を1つのカタログへまとめる。

    Returns: カタログエントリのリスト（スキーマは本モジュール docstring 参照）。
    say が使えない環境でも paid エントリ（あれば）は返る。両方空なら空リスト。
    """
    cfg = cfg or {}
    catalog: List[Dict[str, Any]] = []

    for v in list_say_japanese_voices(_run=_say_run):
        catalog.append({
            "key": "say_{}".format(v["name"].lower()),
            "tier": "free",
            "label": v["label"],
            "gender": v.get("gender", "unknown"),
            "engine": "say",
            "engine_voice_id": v["name"],
            "pitch": v.get("pitch"),
        })

    for v in _fish_voices_from_cfg(cfg):
        catalog.append({
            "key": "fish_{}".format(v["id"][:8]),
            "tier": "paid",
            "label": v["label"],
            "gender": v.get("gender", "unknown"),
            "engine": "fish",
            "engine_voice_id": v["id"],
            "pitch": v.get("pitch"),
            "style": v.get("style"),
        })

    return catalog


_PITCH_ORDER = {"low": 0, "mid": 1, "high": 2}


def _pitch_distance(a: Optional[str], b: Optional[str]) -> int:
    """pitch 文字列の距離（不明は中庸扱い=1）。小さいほど近い。"""
    ia = _PITCH_ORDER.get(a, 1)
    ib = _PITCH_ORDER.get(b, 1)
    return abs(ia - ib)


def tier_default_entry(catalog: List[Dict[str, Any]], tier: str,
                       cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """指定 tier の既定エントリを返す（自動選択の fallback）。

    free: cfg.voice（既定 Kyoko）に対応する say エントリを優先、無ければ先頭。
    paid: config.voices.fish の先頭（=カタログ内 paid の先頭）。
    該当 tier のエントリが無ければ None。
    """
    tier_entries = [e for e in catalog if e.get("tier") == tier]
    if not tier_entries:
        return None
    if tier == "free":
        want = ((cfg or {}).get("voice") or _FREE_DEFAULT_VOICE)
        for e in tier_entries:
            if e.get("engine_voice_id") == want:
                return e
    return tier_entries[0]


def select_voice_auto(narrator_voice: Optional[Dict[str, Any]],
                      catalog: List[Dict[str, Any]], tier: str,
                      cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """参考の narrator_voice（gender_guess/pitch）に最も近い声を tier 内から選ぶ。

    優先順:
      1. gender 一致（narrator が male/female のとき、その性別のエントリだけを候補にする）
      2. pitch 距離が最小
      3. カタログ内の登場順（決定論）
    narrator_voice が無い / gender_guess=unknown のときは tier_default_entry へフォールバック。
    """
    tier_entries = [e for e in catalog if e.get("tier") == tier]
    if not tier_entries:
        return None

    nv = narrator_voice if isinstance(narrator_voice, dict) else None
    gender = (nv or {}).get("gender_guess")
    pitch = (nv or {}).get("pitch")

    if gender not in ("male", "female"):
        return tier_default_entry(catalog, tier, cfg=cfg)

    gender_matches = [e for e in tier_entries if e.get("gender") == gender]
    if not gender_matches:
        # 性別一致が無い（例: paid に male しか登録されていない）→ tier 既定へ
        return tier_default_entry(catalog, tier, cfg=cfg)

    # pitch 距離 → 定番優先 → 登場順 で決定論的に1つ選ぶ
    # （pitch が同点なら Kyoko/Otoya のような定番の自然な声をノベルティ声より優先する）
    def _sort_key(kv):
        i, e = kv
        preferred = 0 if e.get("engine_voice_id") in _SAY_PREFERRED else 1
        return (_pitch_distance(e.get("pitch"), pitch), preferred, i)
    indexed = list(enumerate(gender_matches))
    indexed.sort(key=_sort_key)
    return indexed[0][1]


def _tier_for_cfg(cfg: Dict[str, Any]) -> str:
    """現在の cfg が free tier（say・課金ゼロ）か paid tier（fish_audio）かを判定する。"""
    engine = ((cfg or {}).get("tts") or {}).get("engine")
    return "paid" if engine == "fish_audio" else "free"


def resolve_voice(cfg: Optional[Dict[str, Any]],
                  narrator_voice: Optional[Dict[str, Any]] = None,
                  _say_run=None) -> Dict[str, Any]:
    """cfg.tts.voice_mode（"auto" | カタログ key）と narrator_voice から、使う声を確定する。

    free tier（say）のときは課金ゼロ保証のため paid（fish）エントリを絶対に選ばない
    （auto でも手動 key 指定でも free の中だけから選ぶ）。

    Returns:
      {"key": str, "label": str, "engine": "say"|"fish", "engine_voice_id": str,
       "tier": str, "mode": "auto"|"manual"|"fallback", "gender": str}
    カタログが空（say も fish も無い）の極端な環境では say/Kyoko の合成エントリを返す
    （パイプライン全体を止めないため）。
    """
    cfg = cfg or {}
    tier = _tier_for_cfg(cfg)
    catalog = build_catalog(cfg, _say_run=_say_run)
    voice_mode = ((cfg.get("tts") or {}).get("voice_mode")) or "auto"

    # tier 制限（free は free のみ、paid は free+paid どちらも許容＝安い say を選んでも良い）
    if tier == "free":
        allowed = [e for e in catalog if e.get("tier") == "free"]
    else:
        allowed = list(catalog)

    def _synth_default() -> Dict[str, Any]:
        # カタログが空でも必ず何か返す（Kyoko say）。
        return {
            "key": "say_kyoko", "label": _SAY_LABELS["Kyoko"], "engine": "say",
            "engine_voice_id": _FREE_DEFAULT_VOICE, "tier": "free",
            "mode": "fallback", "gender": "female",
        }

    def _as_result(entry: Dict[str, Any], mode: str) -> Dict[str, Any]:
        return {
            "key": entry.get("key"),
            "label": entry.get("label"),
            "engine": entry.get("engine"),
            "engine_voice_id": entry.get("engine_voice_id"),
            "tier": entry.get("tier"),
            "mode": mode,
            "gender": entry.get("gender", "unknown"),
        }

    if voice_mode and voice_mode != "auto":
        # 手動指定: allowed 内に key があればそれを使う。無ければ fallback。
        for e in allowed:
            if e.get("key") == voice_mode:
                return _as_result(e, "manual")
        default = tier_default_entry(catalog, tier, cfg=cfg)
        return _as_result(default, "fallback") if default else _synth_default()

    # auto
    picked = select_voice_auto(narrator_voice, allowed, tier, cfg=cfg)
    if picked is not None:
        return _as_result(picked, "auto")
    default = tier_default_entry(catalog, tier, cfg=cfg)
    return _as_result(default, "fallback") if default else _synth_default()
