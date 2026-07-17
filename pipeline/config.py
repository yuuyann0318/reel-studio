# -*- coding: utf-8 -*-
"""config.json ローダー。video-auto-editor/pipeline/config.py のパターンを踏襲。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _PROJECT_ROOT / "config.json"

_DEFAULTS = {
    "backend": "mock",
    "ffmpeg_bin": str(_PROJECT_ROOT / "bin" / "ffmpeg"),
    "ffprobe_bin": str(_PROJECT_ROOT / "bin" / "ffprobe"),
    "claude_bin": "/Users/yuuya/.local/bin/claude",
    "claude_model": "claude-fable-5",
    "claude_timeout_sec": 600,
    "resolution": [1080, 1920],
    "aspect": "9:16",
    "target_duration_sec": 30,
    "voice": "Kyoko",
    "default_subtitle_style": "default",  # "default" | "vertical_hook"（Studio新規作成時の既定スタイル）
    "brand_rules": {"ng_words": []},
    "higgsfield": {
        "cli_bin": "higgsfield",
        "model": "seedance_2_0_mini",
        "resolution": "480p",
        "max_credits_per_shot": 10,
        "poll_interval_sec": 5,
        "poll_timeout_sec": 600,
    },
    "cloudapi": {"base_url": "", "api_key_env": "HIGGSFIELD_API_KEY"},
}


def load_config() -> dict:
    """config.json を読み込み、欠けているキーはデフォルトで補完して返す。

    config.json が存在しない/壊れている場合もデフォルトのみで動作継続する。
    """
    data = {}
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    merged = dict(_DEFAULTS)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def project_root() -> Path:
    return _PROJECT_ROOT


def work_dir() -> Path:
    d = _PROJECT_ROOT / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    d = _PROJECT_ROOT / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d
