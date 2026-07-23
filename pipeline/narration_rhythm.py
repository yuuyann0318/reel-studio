# -*- coding: utf-8 -*-
"""R2b F7: ナレーションのリズム写像。

ASR segments（参考ナレーションの発話単位）から、参考動画の話速（文字/秒）と
間（セグメント間のギャップ秒）の統計を取り、下流の director プロンプトに
「参考の話速と間に合わせて narration_jp の文長を書け」の指示 + shot ごとの
目安文字数を injection できるようにする。

Python 3.9 互換・stdlib のみ。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _segment_duration(seg: Dict[str, Any]) -> float:
    s = seg.get("start")
    e = seg.get("end")
    if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
        return 0.0
    d = float(e) - float(s)
    return d if d > 0 else 0.0


def compute_narration_rhythm(
    segments: List[Dict[str, Any]],
    duration_sec: float,
    pause_min_sec: float = 0.35,
) -> Dict[str, Any]:
    """ASR segments から話速統計・間・ポーズ位置を返す。

    Args:
        segments: [{"start": s, "end": e, "text": str}, ...]（ASR 出力）。
        duration_sec: 参考動画の総尺（秒）。
        pause_min_sec: これ以上のギャップを「間 (pause)」として記録する閾値。

    Returns:
        {"chars_per_sec": float, "avg_gap_sec": float, "pause_points": [float,...],
         "segments_count": int, "speech_duration_sec": float}
        segments が空/無効なら chars_per_sec=0, avg_gap_sec=0, pause_points=[] を返す。
    """
    if not segments:
        return {"chars_per_sec": 0.0, "avg_gap_sec": 0.0, "pause_points": [],
                "segments_count": 0, "speech_duration_sec": 0.0}

    total_chars = 0
    total_speech = 0.0
    prev_end: Optional[float] = None
    gaps: List[float] = []
    pause_points: List[float] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text") or ""
        dur = _segment_duration(seg)
        if dur <= 0:
            continue
        total_speech += dur
        total_chars += len([ch for ch in str(text) if ch.strip()])
        s = float(seg.get("start") or 0.0)
        if prev_end is not None:
            gap = s - prev_end
            if gap > 0:
                gaps.append(gap)
                if gap >= float(pause_min_sec):
                    pause_points.append(round(prev_end, 3))
        prev_end = float(seg.get("end") or s + dur)

    chars_per_sec = round(total_chars / total_speech, 3) if total_speech > 0 else 0.0
    avg_gap_sec = round(sum(gaps) / len(gaps), 3) if gaps else 0.0

    return {
        "chars_per_sec": chars_per_sec,
        "avg_gap_sec": avg_gap_sec,
        "pause_points": pause_points,
        "segments_count": len(segments),
        "speech_duration_sec": round(total_speech, 3),
    }


def expected_chars_for_shot(shot_duration_sec: float, chars_per_sec: float) -> int:
    """shot 尺 × 参考話速 → 目安文字数（int）。0 未満は 0。"""
    try:
        v = float(shot_duration_sec) * float(chars_per_sec)
    except (TypeError, ValueError):
        return 0
    return int(max(0, round(v)))
