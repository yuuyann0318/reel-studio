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

import json
import subprocess
import time
import urllib.parse
import urllib.request
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
            # f0_hz: 実試聴（Fish TTSで1文合成→librosa pyin中央値）で実測した基本周波数[Hz]。
            # gender/pitch メタの根拠であり、再現・監査用に保持する（自動選択には pitch を使う）。
            "f0_hz": v.get("f0_hz"),
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
            "f0_hz": v.get("f0_hz"),
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
    # 参考の実測 f0[Hz]（reference_v2.estimate_narrator_voice が付ける値）。
    # ある場合は「同じ pitch ラベル内での近さ」までここで判定できる
    # （female/high が3声あるなら参考 300Hz に一番近い声を選ぶ、等）。
    f0_ref = nv.get("f0_median_hz") if nv else None
    try:
        f0_ref = float(f0_ref) if f0_ref is not None else None
    except (TypeError, ValueError):
        f0_ref = None

    if gender not in ("male", "female"):
        return tier_default_entry(catalog, tier, cfg=cfg)

    gender_matches = [e for e in tier_entries if e.get("gender") == gender]
    if not gender_matches:
        # 性別一致が無い（例: paid に male しか登録されていない）→ tier 既定へ
        return tier_default_entry(catalog, tier, cfg=cfg)

    # 決定論的1件: pitchラベル距離 → f0[Hz]距離 → 定番優先 → 登場順。
    # 参考 f0[Hz] を渡された場合は voice の f0_hz を実距離で突き合わせる
    # （同じ pitch ラベル内の複数女性/男性でも「参考により近い声」に寄せられる）。
    def _f0_gap(entry_f0):
        try:
            if entry_f0 is None or f0_ref is None:
                return 10_000.0  # 情報無しは同点扱い（pitch とは別軸なので大きめの共通値）
            return abs(float(entry_f0) - f0_ref)
        except (TypeError, ValueError):
            return 10_000.0

    def _sort_key(kv):
        i, e = kv
        preferred = 0 if e.get("engine_voice_id") in _SAY_PREFERRED else 1
        return (
            _pitch_distance(e.get("pitch"), pitch),
            _f0_gap(e.get("f0_hz")),
            preferred,
            i,
        )
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


# ---------------------------------------------------------------------------
# Fish Audio 公式APIからの声モデル動的取得（声の調達＝config.voices.fish 拡充の補助ツール）
#
# 公式 List Models: GET https://api.fish.audio/model
#   Query: language, sort_by(score|task_count|created_at), page_size, page_number,
#          title, tag ...  Auth: `Authorization: Bearer <API_KEY>`。
#   Response: {"total": int, "items": [{"_id","title","type","languages",
#              "like_count","task_count", ...}], "has_more": bool}
# 実APIで実在確認済み（2026-07-26, language=ja&sort_by=task_count）。
#
# 注意:
#   - API は gender/pitch を返さない。ここで得るのは「実在する候補ID一覧」まで。
#     gender/pitch は実試聴（Fish TTSで1文合成→f0中央値）で確定し、config.voices.fish
#     に登録する（＝運用者/検証工程の責務。ここで捏造しない）。
#   - build_catalog はオフライン決定論・課金ゼロを保つため、この動的取得を既定では呼ばない。
#     声の棚卸し/補充のときだけ明示的に使う discovery ヘルパである。
# ---------------------------------------------------------------------------

_FISH_MODEL_LIST_URL = "https://api.fish.audio/model"
_FISH_API_KEY_ENV_DEFAULT = "FISH_AUDIO_API_KEY"


def _default_fish_model_get(url: str, api_key: str, timeout_sec: int = 30) -> Dict[str, Any]:
    """GET /model を叩き JSON dict を返す（stdlib urllib のみ・依存追加ゼロ）。テストは _http_get で差替える。"""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer {}".format(api_key))
    resp = urllib.request.urlopen(req, timeout=timeout_sec)
    try:
        raw = resp.read()
    finally:
        resp.close()
    return json.loads(raw.decode("utf-8", "replace"))


def fetch_fish_voices(api_key: Optional[str] = None, language: str = "ja",
                      sort_by: str = "task_count", limit: int = 8,
                      api_key_env: str = _FISH_API_KEY_ENV_DEFAULT,
                      _http_get=None, timeout_sec: int = 30,
                      max_pages: int = 5) -> List[Dict[str, Any]]:
    """Fish Audio 公式APIから language 指定の TTS 声モデルを人気順で上位 limit 件取得する。

    Returns: [{"id","title","languages","task_count","like_count"}, ...]（人気順）。
    API キーが無い / HTTP・パースエラー / 応答が不正のときは **空リスト** を返す
    （呼び出し側は登録済み config.voices.fish にフォールバックすればよい・例外は出さない）。
    gender/pitch は API に無いため含めない（実試聴で確定して登録すること）。

    ページング: /model は tts + svc（歌声変換）混在で返るため、tts だけで limit 件を
    確実に得るには複数ページを追う必要がある（1ページ内で全部 svc の可能性もある）。
    limit まで埋まるか has_more=False か max_pages に達したら止める（安全弁）。
    """
    import os
    key = api_key if api_key is not None else os.environ.get(api_key_env)
    if not key:
        return []
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 8
    getter = _http_get or _default_fish_model_get
    out: List[Dict[str, Any]] = []
    seen = set()
    # svc 混入で欠ける分を吸収するため 1 ページ 20 件（or n×2）で複数ページを追う。
    page_size = max(n * 2, 20)
    for page in range(1, max(1, int(max_pages)) + 1):
        params = urllib.parse.urlencode({
            "language": language, "sort_by": sort_by,
            "page_size": page_size, "page_number": page,
        })
        url = "{}?{}".format(_FISH_MODEL_LIST_URL, params)
        try:
            data = getter(url, key, timeout_sec)
        except Exception:
            break
        if not isinstance(data, dict):
            break
        items = data.get("items")
        if not isinstance(items, list) or not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            # svc（歌声変換）等は除外し tts のみ。type 欠落は許容（後方互換）。
            if it.get("type") not in (None, "tts"):
                continue
            vid = it.get("_id") or it.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            out.append({
                "id": str(vid),
                "title": it.get("title") or "",
                "languages": it.get("languages") or [],
                "task_count": it.get("task_count"),
                "like_count": it.get("like_count"),
            })
            if len(out) >= n:
                return out
        if data.get("has_more") is False:
            break
    return out


def refresh_fish_catalog_cache(cache_path: str, api_key: Optional[str] = None,
                               language: str = "ja", sort_by: str = "task_count",
                               limit: int = 8, api_key_env: str = _FISH_API_KEY_ENV_DEFAULT,
                               _http_get=None, _now=None) -> List[Dict[str, Any]]:
    """fetch_fish_voices の結果を cache_path(JSON) に保存して返す（棚卸し補助）。

    保存形式: {"fetched_at": epoch_sec, "language": str, "voices": [...]}。
    取得0件（キー無し/エラー）なら書き込みはせず [] を返す（古いキャッシュを壊さない）。
    """
    voices = fetch_fish_voices(api_key=api_key, language=language, sort_by=sort_by,
                               limit=limit, api_key_env=api_key_env, _http_get=_http_get)
    if not voices:
        return []
    now = (_now or time.time)()
    payload = {"fetched_at": float(now), "language": language, "voices": voices}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return voices


def load_cached_fish_voices(cache_path: str, max_age_sec: Optional[float] = None,
                            _now=None) -> List[Dict[str, Any]]:
    """refresh_fish_catalog_cache が書いた JSON を読む。無効/期限切れなら []。

    max_age_sec 指定時は fetched_at からの経過がそれを超えていたら [] を返す（要再取得）。
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    voices = data.get("voices")
    if not isinstance(voices, list):
        return []
    if max_age_sec is not None:
        fetched_at = data.get("fetched_at")
        try:
            age = (_now or time.time)() - float(fetched_at)
        except (TypeError, ValueError):
            return []
        if age > float(max_age_sec):
            return []
    return voices
