# -*- coding: utf-8 -*-
"""編集プロファイル（参考動画TTP解析値）のロード。

assets/profiles/<name>.json を基点にロードし、同ディレクトリの manual_drive.json が
存在して非空なら、その内容を浅いマージ（トップレベルキー単位の上書き）で反映する
（manual優先）。manual_drive.json.example はプレースホルダであり load_profile からは
一切参照されない（実際に反映したい場合はユーザーが manual_drive.json としてコピーする）。

スキーマ検証: version/structure/telop/emphasis/audio の必須キーが揃っているかのみを
検証する（値の詳細な型検証まではしない・値は premiere.export_xmeml 側で寛容に扱う）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.config import project_root

REQUIRED_KEYS = ("version", "structure", "telop", "emphasis", "audio")


class ProfileValidationError(ValueError):
    """プロファイルJSONの読み込み失敗、または必須キー欠落時に送出する。"""


def _default_profiles_dir():
    return project_root() / "assets" / "profiles"


def _load_json_file(path):
    """pathのJSONをロードする。存在しなければNone、パース失敗ならProfileValidationError。"""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileValidationError("プロファイルファイルの読み込みに失敗しました: {} ({})".format(path, exc))
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ProfileValidationError("プロファイルJSONの構文が不正です: {} ({})".format(path, exc))


def _validate_schema(profile, source):
    if not isinstance(profile, dict):
        raise ProfileValidationError("プロファイルはオブジェクト(dict)である必要があります: source={}".format(source))
    missing = [k for k in REQUIRED_KEYS if k not in profile]
    if missing:
        raise ProfileValidationError(
            "プロファイルに必須キーがありません: {} (source={})".format(missing, source)
        )


def _shallow_merge(base, override):
    """トップレベルキー単位の浅いマージ（overrideに存在するキーはbaseの値を丸ごと置き換える）。"""
    merged = dict(base)
    for key, value in override.items():
        merged[key] = value
    return merged


def load_profile(name="ttp_reference", profiles_dir=None):
    """assets/profiles/<name>.json をロードし、manual_drive.json（存在し非空なら）でマージして返す。

    Args:
        name: プロファイル名（拡張子なし）。既定 "ttp_reference"。
        profiles_dir: プロファイル格納ディレクトリ（既定 project_root()/assets/profiles）。
                       テストで一時ディレクトリを指定できるようにするための引数。

    Returns:
        dict: マージ後のプロファイル（version/structure/telop/emphasis/audio を必ず含む）。

    Raises:
        ProfileValidationError: ベースプロファイルが存在しない/JSON不正/必須キー欠落、
                                 またはマージ後のスキーマが不正な場合。
    """
    pdir = Path(profiles_dir) if profiles_dir is not None else _default_profiles_dir()
    base_path = pdir / "{}.json".format(name)

    base_profile = _load_json_file(base_path)
    if base_profile is None:
        raise ProfileValidationError("プロファイルが見つかりません: {}".format(base_path))
    _validate_schema(base_profile, str(base_path))

    manual_path = pdir / "manual_drive.json"
    manual_override = _load_json_file(manual_path)
    if isinstance(manual_override, dict) and manual_override:
        merged = _shallow_merge(base_profile, manual_override)
        _validate_schema(merged, str(manual_path))
        return merged

    return base_profile
