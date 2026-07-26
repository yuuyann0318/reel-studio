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
    # 空文字 or 未設定なら claude_runner 側で `claude` コマンドを PATH 解決する。
    "claude_bin": "",
    "claude_model": "claude-fable-5",
    "claude_timeout_sec": 600,
    "resolution": [1080, 1920],
    "aspect": "9:16",
    "target_duration_sec": 30,
    "voice": "Kyoko",
    "default_subtitle_style": "default",  # "default" | "vertical_hook"（Studio新規作成時の既定スタイル）
    "director_quality": "supreme",  # "supreme"（3段多段生成） | "single"（従来の一発出し） | "supreme_plus"（品質最優先）
    # BGM の付け方。"auto" = 従来どおり参考動画のムード/テンポに合わせて自前ライブラリから
    # 選曲する（TTP準拠）。"none" = BGM を一切付けない（後付け派向け。編集ソフトで自分で
    # BGM を足す前提）。none のとき render/jobs は BGM 入力・音量カーブ・ダッキング/ディップ計算を
    # 完全にスキップする。CLI は --bgm none|auto で1回だけ上書きできる。
    "audio": {"bgm_mode": "auto"},
    "brand_rules": {"ng_words": []},
    # F12: director 側の細部プリセット（supreme_plus で上書きされる）。既存挙動と互換の
    # ため空 dict を既定にし、build_shot_skeleton は config.director.min_shot_sec が
    # 与えられているときのみそれを使う。プロンプト末尾に注入する「品質最優先」文言も
    # config.director.quality_directive として持つ（未指定なら注入しない）。
    "director": {
        "min_shot_sec": None,        # None なら director 内既定 (_SKELETON_MIN_SHOT_SEC=1.2)
        "stages": None,              # None なら quality に従う（supreme=write+polish）
        "quality_directive": None,   # 追加のプロンプト指示文（品質最優先など）
        # R2a F5: ビートスナップ（reference_spec.music.beat_times に shot 境界を吸着させる）
        # None（既定）は director 側で quality によって自動決定する:
        #   supreme_plus → True（品質最優先 = beat_snap ON） / それ以外 → False。
        # 明示的に True/False を指定すれば director 側の自動判定より優先される。
        "beat_snap_enabled": None,
        "beat_snap_tolerance_ms": 250,
    },
    "higgsfield": {
        "cli_bin": "higgsfield",
        "model": "seedance_2_0_mini",
        "resolution": "480p",
        "max_credits_per_shot": 10,
        "poll_interval_sec": 5,
        "poll_timeout_sec": 600,
    },
    # visual: 映像生成の横断オプション。
    #   no_text_in_video: ①予防 — 生成プロンプトに「映像内へ文字を描かない」negative 指示を
    #     常時注入する（既定 True・False で従来動作）。AIが描く偽文字/崩れ日本語の予防。
    #   persona_consistency: 人物 shot の identity 統一（既定 True）。
    #   text_check: ②検知 — 生成クリップ（テロップ焼き込み前）の代表フレームを vision で検査し、
    #     映像内文字/文字化けの疑いを report / project.json に記録する（作り直しはユーザー判断）。
    "visual": {
        "no_text_in_video": True,
        "persona_consistency": True,
        "text_check": {
            "enabled": True,
            "max_vision_calls": 3,     # vision 呼び出しの上限回数
            "vision_batch_size": 10,   # 1 呼び出しあたりのフレーム枚数
            "vision_timeout_sec": 600,
        },
    },
    "cloudapi": {"base_url": "", "api_key_env": "HIGGSFIELD_API_KEY"},
    "tts": {
        "engine": "fish_audio",
        # 声の選び方: "auto" = 参考動画の話者推定(narrator_voice)に近い声を自動選択。
        # カタログの key（例 "say_kyoko" / "fish_xxxxxxxx"）を書けばその声で固定する。
        "voice_mode": "auto",
        "fish_audio": {
            "api_key_env": "FISH_AUDIO_API_KEY",
            "model": "s2.1-pro-free",
            "reference_id": "",
            "format": "wav",
            "timeout_sec": 60,
        },
    },
    # 声カタログ（有料 tier = Fish Audio）の登録制リスト。実在する voice(reference)ID のみ
    # 登録すること（存在しないIDを書くと Fish 側で無効になり say へフォールバックする）。
    # 各要素: {"id": <Fish reference_id>, "label": <平易な日本語>, "gender": male|female,
    #          "style": <任意>, "pitch": low|mid|high(任意)}。空なら auto/paid は
    #          config.json tts.fish_audio.reference_id（単一声）へ実質フォールバックする。
    # 無料 tier（say）は実機の `say -v ?` から自動列挙するため登録不要。
    "voices": {"fish": []},
    "product_images": {
        "max_images": 6,
        "min_short_side": 400,
        "local_dir": "assets/products/inbox",
        "timeout_sec": 20,
    },
    "reference": {
        "cache_dir": "assets/reference_cache",
        "yt_dlp_bin": "bin/yt-dlp_macos",
        "download_timeout_sec": 120,
        "asr_timeout_sec": 120,
        "max_video_sec": 180,
        "scene_threshold": 0.30,
        # R2a F1: 複数閾値アンサンブル。空/未指定なら scene_threshold の単一手法。
        "scene_thresholds": None,
        "scene_use_pyscenedetect": False,
        "pyscenedetect_threshold": 27.0,
        "scene_merge_window_sec": 0.15,
        "max_vision_calls": 4,
        "max_frames": 40,
        "vision_batch_size": 10,
        "vision_timeout_sec": 600,
        "onset_jump_db": 6.0,
        "onset_window_samples": 2048,
        # 声TTP: 発話区間から話者の f0 中央値・話速を推定し spec.narrator_voice を付与する
        # （ASR 成功 かつ segments 非空のときのみ・librosa 任意依存）。False で無効化。
        "narrator_voice_enabled": True,
        # R2a F5: BPM/beat 抽出（librosa）。False で無効化。
        "music_extract_enabled": True,
        # 3秒ごと密ストーリーボード解析（診断 P1-6 / ユーザー要求「3秒に1回、1枚1枚を
        # 忠実に認識」）。None=director_quality が supreme_plus / ttps のとき自動 ON、
        # それ以外 OFF。明示的に True/False で上書き可。
        "storyboard_enabled": None,
        "storyboard_interval_sec": 3.0,     # フレーム抽出の一律間隔（秒）
        "storyboard_max_vision_calls": 8,   # storyboard 用 vision 呼び出しの上限
        "storyboard_batch_size": 10,        # 1 vision 呼び出しあたりの画像枚数
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """dict を再帰的にマージする（overlay 優先。overlay の値が dict なら再帰、それ以外は上書き）。"""
    out = dict(base)
    if not isinstance(overlay, dict):
        return out
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_local_overlays() -> dict:
    """`config.local/*.json` をキーの辞書順で読み、順に deep_merge して1つの overlay を返す。

    F12: `config.local/quality_max.json` を新設して `director_quality: "supreme_plus"` 等を
    宣言的に流し込むための機構。config.local/ は .gitignore 済みでコミット対象外。
    ファイルが存在しない/壊れている場合は空 dict（＝上書きなし）を返し、静かに続行する。
    """
    local_dir = _PROJECT_ROOT / "config.local"
    if not local_dir.is_dir():
        return {}
    overlay: dict = {}
    try:
        json_files = sorted([p for p in local_dir.iterdir() if p.is_file() and p.suffix == ".json"])
    except Exception:
        return {}
    for p in json_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            overlay = _deep_merge(overlay, data)
    return overlay


def load_config() -> dict:
    """config.json を読み込み、欠けているキーはデフォルトで補完して返す。

    さらに `config.local/*.json` があれば辞書順に deep_merge で上書きする（F12: 品質最優先
    プリセット `quality_max.json` 等をここで拾う）。config.local は .gitignore 済み。
    config.json が存在しない/壊れている場合もデフォルトのみで動作継続する。
    """
    data = {}
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    merged = _deep_merge(_DEFAULTS, data if isinstance(data, dict) else {})
    overlay = _load_local_overlays()
    if overlay:
        merged = _deep_merge(merged, overlay)
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
