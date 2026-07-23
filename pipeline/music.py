# -*- coding: utf-8 -*-
"""音楽ビート（BPM / beat_times / downbeats）の抽出。

R2a F5: 参考動画の音声から BPM と拍時刻列を取り、reference_spec v2 の music フィールドに
入れる。director 側の build_shot_skeleton は beat_snap モード時にこの beat_times へ
shot 境界を吸着させる。

依存の扱い:
  - librosa があれば librosa.beat.beat_track を使う（本命経路）。
  - librosa が無い/インポート失敗の場合は、confidence=0 の空 features を返し
    warnings に理由を記録する（呼び出し側は features["bpm"]==0 で「未検出」と扱う）。

Python 3.9 互換構文のみ・stdlibのみで動く（librosa は任意依存）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _try_import_librosa():
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore
        return librosa, np
    except Exception:
        return None, None


def _beat_confidence_from_bpm(bpm: float, beats_count: int, duration_sec: float) -> float:
    """簡易 confidence 推定。BPM が典型範囲(60〜200)にあり、beats_count が
    duration_sec × bpm / 60 と概ね一致すれば高い値。ここでは以下の合成で決める:

    - BPM 範囲スコア: 60〜180 で 1.0、60〜200 で 0.7、それ以外で 0.4
    - 密度スコア: 期待拍数 = duration_sec * bpm / 60。abs(diff)/expected ≤ 0.15 で 1.0、
      ≤ 0.30 で 0.7、それ以外で 0.4
    - 掛け合わせで最終値（0.0〜1.0）
    """
    if bpm <= 0 or duration_sec <= 0 or beats_count <= 0:
        return 0.0
    if 60.0 <= bpm <= 180.0:
        bpm_score = 1.0
    elif 40.0 <= bpm <= 200.0:
        bpm_score = 0.7
    else:
        bpm_score = 0.4
    expected = max(1.0, duration_sec * bpm / 60.0)
    density = abs(beats_count - expected) / expected
    if density <= 0.15:
        density_score = 1.0
    elif density <= 0.30:
        density_score = 0.7
    else:
        density_score = 0.4
    return round(bpm_score * density_score, 3)


def extract_music_features_librosa(audio_path: str, sr: int = 22050) -> Dict[str, Any]:
    """librosa で BPM / beat_times / downbeats(近似) を抽出する（本命経路）。

    downbeats は librosa の公式APIには無いため、beat_times を4拍1小節と仮定して
    先頭から4拍おきに切ったものを近似で返す（fallback。ジャンル依存）。
    """
    librosa, np = _try_import_librosa()
    if librosa is None:
        return {"bpm": 0.0, "beat_times": [], "downbeats": [], "confidence": 0.0,
                "engine": "unavailable", "error": "librosa import failed"}
    try:
        # mono 読み込み。sr は 22050 で軽量化（BPM検出には十分）。
        y, sr_used = librosa.load(audio_path, sr=sr, mono=True)
        duration_sec = float(len(y)) / float(sr_used) if sr_used else 0.0
        # librosa.beat.beat_track は tempo (BPM) と beat frames を返す。
        # tightness を上げると BPM の一貫性重視、下げるとローカルな tempo 変化に追従。
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr_used, tightness=100)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr_used)
        try:
            beat_list = [float(t) for t in beat_times.tolist()]
        except Exception:
            beat_list = [float(t) for t in list(beat_times)]
        try:
            bpm_val = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)
        except Exception:
            bpm_val = float(tempo)
        # downbeats 近似: 4拍1小節と仮定して先頭から4拍おき（ジャンルによっては 3拍子など
        # 実態と合わないが、confidence を控えめに返すことで下流で信頼度判断させる）。
        downbeats = beat_list[::4] if beat_list else []
        confidence = _beat_confidence_from_bpm(bpm_val, len(beat_list), duration_sec)
        return {
            "bpm": round(bpm_val, 2),
            "beat_times": [round(t, 4) for t in beat_list],
            "downbeats": [round(t, 4) for t in downbeats],
            "confidence": confidence,
            "engine": "librosa",
            "duration_sec": round(duration_sec, 3),
        }
    except Exception as exc:
        return {
            "bpm": 0.0, "beat_times": [], "downbeats": [], "confidence": 0.0,
            "engine": "librosa", "error": "librosa failed: {}".format(str(exc)[:200]),
        }


def extract_music_features(audio_path: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """公開エントリ。cfg["music"]["sr"] などで挙動を調整可。

    Returns:
        {"bpm": float, "beat_times": [float,...], "downbeats": [float,...],
         "confidence": 0.0〜1.0, "engine": str, "error"?: str}
    """
    cfg = cfg or {}
    music_cfg = cfg.get("music") or {}
    sr = int(music_cfg.get("sr", 22050))
    return extract_music_features_librosa(audio_path, sr=sr)


# ---------------------------------------------------------------------------
# beat_snap: shot 境界を最寄り拍に吸着する（director から呼ばれる純関数）
# ---------------------------------------------------------------------------

def _nearest_beat(t: float, beats: List[float]) -> Optional[float]:
    if not beats:
        return None
    lo = 0
    hi = len(beats) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if beats[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    # lo は t 以上の最初の位置。左右比較で近い方を返す。
    cand: List[Tuple[float, float]] = []
    if lo > 0:
        cand.append((abs(t - beats[lo - 1]), beats[lo - 1]))
    if lo < len(beats):
        cand.append((abs(t - beats[lo]), beats[lo]))
    if not cand:
        return None
    return min(cand, key=lambda x: x[0])[1]


def snap_boundaries_to_beats(
    boundaries: List[float],
    beat_times: List[float],
    tolerance_sec: float = 0.25,
    min_shot_sec: float = 0.5,
) -> Tuple[List[float], Dict[str, int]]:
    """内部境界 boundaries[1..N-1] を beat_times の最寄り拍へ吸着させる。

    - boundaries[0] と boundaries[-1] は保持（開始 0 / 終端 duration）。
    - 吸着幅は ±tolerance_sec 以内のみ有効（それ以上離れた境界は動かさない）。
    - 隣接境界と min_shot_sec 以下になるスナップは棄却する（min_shot 保証）。
    - 単調非減少を保つ（前後の boundary との整合を保つ）。

    Returns:
        (snapped_boundaries, stats) — stats={snapped, skipped_far, skipped_min_shot}
    """
    if not boundaries or len(boundaries) < 3 or not beat_times:
        return list(boundaries), {"snapped": 0, "skipped_far": 0, "skipped_min_shot": 0}

    sorted_beats = sorted(beat_times)
    out = list(boundaries)
    stats = {"snapped": 0, "skipped_far": 0, "skipped_min_shot": 0}
    for i in range(1, len(out) - 1):
        cur = out[i]
        near = _nearest_beat(cur, sorted_beats)
        if near is None:
            continue
        if abs(near - cur) > tolerance_sec:
            stats["skipped_far"] += 1
            continue
        # 単調性・min_shot チェック（左右の境界との差が min_shot_sec を割らないこと）
        prev_b = out[i - 1]
        next_b = out[i + 1]
        if near - prev_b < min_shot_sec - 1e-6:
            stats["skipped_min_shot"] += 1
            continue
        if next_b - near < min_shot_sec - 1e-6:
            stats["skipped_min_shot"] += 1
            continue
        # 反映
        if abs(near - cur) > 1e-6:
            out[i] = round(float(near), 4)
            stats["snapped"] += 1
    return out, stats
