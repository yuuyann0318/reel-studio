# -*- coding: utf-8 -*-
"""pipeline/edit_profile.py: assets/profiles/ttp_reference.json の "edit" セクション読み込みの検証。

manifest/ttp_referenceの実ファイルパスをmonkeypatchで一時ファイルへ差し替え、
壊れた/欠けた入力でも安全にデフォルトへフォールバックすることを確認する。
実ffmpegは使わない。
"""
import json

import pytest

from pipeline import edit_profile


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_load_edit_profile_returns_defaults_when_edit_section_missing(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    _write_json(ref_path, {"version": 1})
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["enabled"] is True
    assert profile["cut_sfx"]["gain_db"] == -18
    assert profile["cut_sfx"]["min_interval_sec"] == 1.5
    assert profile["punch_in"]["max_zoom"] == 1.08
    assert profile["punch_in"]["skip_if_backend"] == "mock"
    assert profile["bgm_curve"]["hook_gain_db"] == -10
    assert profile["first_shot_impact"]["scale_pop"] is True


def test_load_edit_profile_missing_reference_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", tmp_path / "does_not_exist.json")

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["enabled"] is True
    assert profile["punch_in"]["pattern"] == ["zoom_in_slow", "static", "zoom_in_fast", "pan_subtle"]


def test_load_edit_profile_malformed_json_falls_back_to_defaults(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    ref_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["bgm_curve"]["enabled"] is True


def test_load_edit_profile_ignores_non_dict_edit_section(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    _write_json(ref_path, {"edit": "not-a-dict"})
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["gain_db"] == -18


def test_load_edit_profile_partial_override_merges_with_defaults(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    _write_json(ref_path, {"edit": {"cut_sfx": {"gain_db": -6}}})
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["gain_db"] == -6
    assert profile["cut_sfx"]["min_interval_sec"] == 1.5  # 上書きしなかったキーはデフォルト維持
    assert profile["punch_in"]["max_zoom"] == 1.08  # 他セクションは無傷


def test_load_edit_profile_disabled_section_is_respected(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    _write_json(ref_path, {"edit": {"punch_in": {"enabled": False}}})
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["punch_in"]["enabled"] is False


def test_default_cut_sfx_file_picks_transition_label_from_manifest():
    """本物のassets/sfx/manifest.jsonには「場面転換」ラベルのwhoosh_01.wavがある。"""
    assert edit_profile._default_cut_sfx_file() == "whoosh_01.wav"


def test_default_cut_sfx_file_falls_back_when_manifest_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(edit_profile, "_SFX_MANIFEST_PATH", tmp_path / "no_manifest.json")

    assert edit_profile._default_cut_sfx_file() == edit_profile._FALLBACK_CUT_SFX_FILE


def test_default_cut_sfx_file_falls_back_when_no_transition_label(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, [{"file": "click_01.wav", "label_jp": "クリック（タップ/操作）"}])
    monkeypatch.setattr(edit_profile, "_SFX_MANIFEST_PATH", manifest_path)

    assert edit_profile._default_cut_sfx_file() == edit_profile._FALLBACK_CUT_SFX_FILE


def test_load_edit_profile_resolved_path_points_to_existing_sfx_file():
    """デフォルト設定では実在するassets/sfx/whoosh_01.wavへ解決される。"""
    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["resolved_path"] is not None
    assert profile["cut_sfx"]["resolved_path"].endswith("whoosh_01.wav")


def test_load_edit_profile_resolved_path_none_when_file_does_not_exist(tmp_path, monkeypatch):
    ref_path = tmp_path / "ttp_reference.json"
    _write_json(ref_path, {"edit": {"cut_sfx": {"file": "nonexistent_sfx_xyz.wav"}}})
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["resolved_path"] is None


def test_load_edit_profile_reads_real_ttp_reference_edit_section():
    """実物のassets/profiles/ttp_reference.jsonに追記した"edit"セクションが読める。"""
    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["file"] == "whoosh_01.wav"
    assert profile["bgm_curve"]["fade_out_sec"] == 1.5
    assert profile["first_shot_impact"]["enabled"] is True
