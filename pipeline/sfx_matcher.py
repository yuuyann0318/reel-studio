# -*- coding: utf-8 -*-
"""R2b F6: SE 音色マッチング（MFCC 類似度）。

参考動画のSEオンセット近傍 (±win_sec) をライブラリの各SFXファイルと MFCC で
比較し、cos 類似度が最も高いファイルを family 内から選ぶ。

処理は 2 相に分かれる:
  1. `compute_reference_event_mfccs(audio_path, events, cfg)`:
     参考動画側の SE イベントごとに、その t 秒 ± win_sec の音声窓を librosa で読み
     MFCC(13次元) の時間平均ベクトルを計算して events[i]["timbre_mfcc"] に載せる。
     spec.sfx_events[] のフィールドとして永続化する。
  2. `pick_best_by_timbre(target_mfcc, family_files, manifest_mfcc_cache, ...)`:
     ライブラリ側の各 sfx ファイルの MFCC (事前計算 or lazy キャッシュ) と cos 類似度を
     比較し、最も近いファイル辞書を返す。sfx_planner がイベントごとに呼ぶ。

librosa 未導入時: `compute_reference_event_mfccs` は空リストの mfcc を返し、
`pick_best_by_timbre` は 1件目を返す（従来の決定論選定に戻る）。

Python 3.9 互換。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _try_import_librosa():
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore
        return librosa, np
    except Exception:
        return None, None


_DEFAULT_N_MFCC = 13
_DEFAULT_WIN_SEC = 0.20
_DEFAULT_SR = 22050


def _mean_mfcc_from_signal(librosa, np, y, sr: int, n_mfcc: int = _DEFAULT_N_MFCC) -> List[float]:
    if y is None or len(y) < int(sr * 0.03):  # 30ms 未満は情報が足りない
        return []
    try:
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc)
        mean_vec = mfcc.mean(axis=1)
        return [round(float(v), 4) for v in mean_vec.tolist()]
    except Exception:
        return []


def compute_event_mfcc_at(
    audio_path: str,
    t_sec: float,
    win_sec: float = _DEFAULT_WIN_SEC,
    sr: int = _DEFAULT_SR,
    n_mfcc: int = _DEFAULT_N_MFCC,
) -> List[float]:
    """t_sec ± win_sec の音声窓を audio_path から読み、MFCC 時間平均を返す。

    librosa 未導入 / 読み込み失敗時は []（呼び出し側は fallback）。
    """
    librosa, np = _try_import_librosa()
    if librosa is None:
        return []
    if not audio_path or not os.path.exists(audio_path):
        return []
    try:
        start = max(0.0, float(t_sec) - float(win_sec))
        dur = max(0.05, float(win_sec) * 2.0)
        y, sr_used = librosa.load(audio_path, sr=sr, mono=True, offset=start, duration=dur)
        return _mean_mfcc_from_signal(librosa, np, y, sr_used or sr, n_mfcc=n_mfcc)
    except Exception:
        return []


def compute_reference_event_mfccs(
    audio_path: str,
    events: List[Dict[str, Any]],
    win_sec: float = _DEFAULT_WIN_SEC,
    sr: int = _DEFAULT_SR,
    n_mfcc: int = _DEFAULT_N_MFCC,
) -> List[Dict[str, Any]]:
    """spec.sfx_events を受け、各 event.t 近傍の MFCC を timbre_mfcc として付ける。

    純関数的に **新しいリスト** を返す（元の events は破壊しない）。
    audio_path が読めない / librosa 未導入なら timbre_mfcc=[] を付けて返す。
    """
    out: List[Dict[str, Any]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            out.append(ev)
            continue
        t = ev.get("t")
        if not isinstance(t, (int, float)):
            out.append(ev)
            continue
        merged = dict(ev)
        mfcc = compute_event_mfcc_at(audio_path, float(t), win_sec=win_sec, sr=sr, n_mfcc=n_mfcc)
        # 圧縮: 13次元をそのまま保存（各 round 4桁）。3.9互換の list[float]。
        merged["timbre_mfcc"] = mfcc
        out.append(merged)
    return out


def compute_file_mfcc(
    sfx_path: str,
    sr: int = _DEFAULT_SR,
    n_mfcc: int = _DEFAULT_N_MFCC,
    max_dur_sec: float = 2.0,
) -> List[float]:
    """1 SFXファイル全体（先頭 max_dur_sec 秒）の MFCC 時間平均を返す。"""
    librosa, np = _try_import_librosa()
    if librosa is None:
        return []
    if not sfx_path or not os.path.exists(sfx_path):
        return []
    try:
        y, sr_used = librosa.load(sfx_path, sr=sr, mono=True, duration=max_dur_sec)
        return _mean_mfcc_from_signal(librosa, np, y, sr_used or sr, n_mfcc=n_mfcc)
    except Exception:
        return []


def _cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += float(x) * float(y)
        na += float(x) * float(x)
        nb += float(y) * float(y)
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class SfxTimbreCache:
    """SFXファイル別 MFCC の永続キャッシュ。

    - コンストラクタで cache_path (JSON) を渡す。存在すれば load、なければ空dict。
    - `mfcc_for(sfx_path)` で MFCC を返す（初回計算・以後キャッシュ）。
    - `save()` で JSON へ書き出す（呼び出し側任意）。

    librosa 未導入時は常に [] を返し、キャッシュには何も書き込まない。
    """

    def __init__(self, cache_path: Optional[str] = None,
                 sr: int = _DEFAULT_SR, n_mfcc: int = _DEFAULT_N_MFCC):
        self.cache_path = cache_path
        self.sr = int(sr)
        self.n_mfcc = int(n_mfcc)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
            except Exception:
                self._cache = {}

    def _key(self, sfx_path: str) -> str:
        # 相対 basename でキー化（絶対パスが環境で変わっても再利用できるように）
        return os.path.basename(str(sfx_path))

    def mfcc_for(self, sfx_path: str) -> List[float]:
        key = self._key(sfx_path)
        entry = self._cache.get(key) or {}
        mfcc = entry.get("mfcc")
        if isinstance(mfcc, list) and mfcc:
            return list(mfcc)
        computed = compute_file_mfcc(sfx_path, sr=self.sr, n_mfcc=self.n_mfcc)
        if computed:
            self._cache[key] = {"mfcc": computed}
            self._dirty = True
        return list(computed)

    def save(self) -> None:
        if not self._dirty or not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            self._dirty = False
        except Exception:
            pass


def pick_best_by_timbre(
    target_mfcc: Sequence[float],
    family_entries: List[Dict[str, Any]],
    cache: Optional[SfxTimbreCache] = None,
    avoid_file: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """family_entries の中で target_mfcc と cos 類似度が最大のファイル辞書を返す。

    family_entries: manifest から絞り込んだ [{"file": path, ...}, ...]。
    target_mfcc が空 or cache が None（librosa 未導入時など）は先頭要素を返す
    （呼び出し側の決定論経路にフォールバック）。
    avoid_file が指定されたら、そのファイルは連続選定を避けるため候補から除く
    （候補が1件しか無ければ許容する）。
    """
    if not family_entries:
        return None
    # 連続同音回避
    candidates = family_entries
    if avoid_file and len(family_entries) > 1:
        pruned = [e for e in family_entries if e.get("file") != avoid_file]
        if pruned:
            candidates = pruned
    if not target_mfcc or cache is None:
        return candidates[0]
    best = None
    best_sim = -2.0
    for entry in candidates:
        file_path = entry.get("file") or ""
        mfcc = cache.mfcc_for(file_path) if file_path else []
        sim = _cosine_sim(target_mfcc, mfcc)
        if sim > best_sim:
            best_sim = sim
            best = entry
    return best if best is not None else candidates[0]
