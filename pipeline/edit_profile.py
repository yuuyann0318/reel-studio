# -*- coding: utf-8 -*-
"""編集プロファイル（assets/profiles/ttp_reference.json の "edit" セクション）のロード。

プロ編集の要素（カット点のSE・パンチイン(緩急ズーム)・BGM音量カーブ・フック冒頭の
インパクト）をJSONから読み込み、render.py の filtergraph 構築へ渡す既定値付き辞書を返す。
ttp_reference.json 自体が存在しない/壊れている/"edit"セクションが無い場合も、
pipeline.config.load_config() と同じ「デフォルトで補完して動作継続する」方針で
安全にフォールバックする。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.config import project_root

_TTP_REFERENCE_PATH = project_root() / "assets" / "profiles" / "ttp_reference.json"
_SFX_MANIFEST_PATH = project_root() / "assets" / "sfx" / "manifest.json"
_SFX_DIR = project_root() / "assets" / "sfx"

# assets/sfx/manifest.json の label_jp にこの文字列を含むエントリを
# 「カット点の場面転換SE」の既定として選ぶ（whoosh_01.wav = 「ホワッシュ（場面転換）」）。
_TRANSITION_LABEL_HINT = "場面転換"
_FALLBACK_CUT_SFX_FILE = "whoosh_01.wav"

DEFAULT_EDIT_PROFILE = {
    "cut_sfx": {
        "enabled": True,
        "file": None,  # None -> _default_cut_sfx_file() で manifest から解決する
        "gain_db": -18,
        "min_interval_sec": 1.5,
    },
    "punch_in": {
        "enabled": True,
        "pattern": ["zoom_in_slow", "static", "zoom_in_fast", "pan_subtle"],
        "max_zoom": 1.08,
        "skip_if_backend": "mock",
    },
    "bgm_curve": {
        "enabled": True,
        "hook_gain_db": -10,
        "body_gain_db": -14,
        "cta_gain_db": -12,
        "fade_out_sec": 1.5,
    },
    "first_shot_impact": {
        "enabled": True,
        "scale_pop": True,
    },
}


def _default_cut_sfx_file():
    """assets/sfx/manifest.json から「場面転換」系のSFXファイル名を選ぶ。

    manifestが読めない/該当ラベルが無い場合は既知の既定ファイル名にフォールバックする
    （SFX一式の差し替え・並べ替えでmanifest構造が変わっても壊れないようにするため）。
    """
    try:
        manifest = json.loads(_SFX_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _FALLBACK_CUT_SFX_FILE
    if not isinstance(manifest, list):
        return _FALLBACK_CUT_SFX_FILE
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        if _TRANSITION_LABEL_HINT in (entry.get("label_jp") or ""):
            f = entry.get("file")
            if f:
                return f
    return _FALLBACK_CUT_SFX_FILE


def _deep_copy_defaults():
    return json.loads(json.dumps(DEFAULT_EDIT_PROFILE))


def _merge_section(defaults_section, override_section):
    """1セクション分（例: "cut_sfx"）を、defaultsをベースにoverrideのキーだけ上書きしてマージする。
    override_sectionが辞書でなければdefaults_sectionをそのまま返す（壊れた入力への耐性）。
    """
    if not isinstance(override_section, dict):
        return dict(defaults_section)
    merged = dict(defaults_section)
    merged.update(override_section)
    return merged


def _merge_edit_profile(overrides):
    defaults = _deep_copy_defaults()
    defaults["cut_sfx"]["file"] = _default_cut_sfx_file()

    if not isinstance(overrides, dict):
        overrides = {}

    merged = {}
    for section_name, section_defaults in defaults.items():
        merged[section_name] = _merge_section(section_defaults, overrides.get(section_name))
    return merged


def _resolve_cut_sfx_path(cut_sfx):
    """cut_sfx.file（相対ファイル名）を assets/sfx/ 配下の実パスへ解決する。
    ファイルが存在しなければ None（呼び出し側はNoneならカットSEをスキップする）。
    """
    file_name = cut_sfx.get("file")
    if not file_name:
        return None
    p = Path(file_name)
    resolved = p if p.is_absolute() else (_SFX_DIR / file_name)
    return str(resolved) if resolved.exists() else None


def load_edit_profile(cfg=None):
    """ttp_reference.json の "edit" セクションを読み、既定値で補完して返す。

    cfg引数は他のローダー（load_config等）とインターフェースを揃えるために受け取るが、
    現状は未使用（将来プロジェクトごとの上書きに拡張する余地を残すため）。
    戻り値の各section dictには "resolved_path"（cut_sfxのみ・実パスまたはNone）を追加する。
    """
    raw_edit = None
    try:
        data = json.loads(_TTP_REFERENCE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_edit = data.get("edit")
    except Exception:
        raw_edit = None

    merged = _merge_edit_profile(raw_edit)
    merged["cut_sfx"]["resolved_path"] = _resolve_cut_sfx_path(merged["cut_sfx"])
    return merged
