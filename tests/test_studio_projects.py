# -*- coding: utf-8 -*-
"""studio/server/projects.py の単体テスト（永続化・plan検証）。

PROJECTS_ROOTをtmp_pathへ差し替えて実プロジェクトディレクトリを汚さない。
"""
from pathlib import Path

import pytest

from studio.server import projects


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_project_with_clip():
    project = projects.create_project("テストテーマ", 20.0, "mock", status="ready")
    clip_dir = projects.clips_dir(project["id"])
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / "s1.mp4"
    clip_path.write_bytes(b"fake-mp4-bytes")
    return project


def _base_shot(project, clip_path=None, source_duration=5.0, start=0.0, end=5.0):
    """正規形式('projects/<id>/clips/s1.mp4')のclip_pathを持つ最小ショットを組み立てる。"""
    if clip_path is None:
        clip_path = projects.media_relpath_for_clip(project["id"], "s1.mp4")
    return {
        "id": "s1", "order": 0, "enabled": True, "prompt": "abstract bg", "caption": "テストキャプション",
        "clip_path": clip_path, "source_duration": source_duration, "trim": {"start": start, "end": end},
    }


def test_create_project_writes_project_json():
    project = _make_project_with_clip()
    loaded = projects.get_project(project["id"])
    assert loaded is not None
    assert loaded["theme"] == "テストテーマ"
    assert loaded["status"] == "ready"
    assert loaded["plan"]["shots"] == []


def test_list_projects_returns_summary():
    project = _make_project_with_clip()
    summaries = projects.list_projects()
    ids = [s["id"] for s in summaries]
    assert project["id"] in ids


def test_validate_plan_ok_minimal():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "こんにちは", "bgm": None, "sfx": [], "subtitle_style": {}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["shots"][0]["id"] == "s1"
    assert normalized["subtitle_style"] == projects.DEFAULT_SUBTITLE_STYLE


def test_validate_plan_rejects_trim_out_of_range():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project, start=0.0, end=999.0)], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert normalized is None
    assert any("trim範囲" in e for e in errors)


def test_validate_plan_rejects_trim_start_gte_end():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project, start=3.0, end=3.0)], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("trim範囲" in e for e in errors)


def test_validate_plan_rejects_missing_clip_file():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project, clip_path=projects.media_relpath_for_clip(project["id"], "does_not_exist.mp4"))], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("存在しません" in e for e in errors)


def test_validate_plan_rejects_duplicate_shot_ids():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project), _base_shot(project)], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("重複" in e for e in errors)


def test_validate_plan_rejects_ng_word_in_caption():
    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["caption"] = "絶対稼げる副業"
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("コンプライアンス違反" in e for e in errors)


def test_validate_plan_rejects_unknown_bgm_file():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "bgm": {"file": "no_such_file.mp3"}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("plan.bgm.file" in e for e in errors)


def test_validate_plan_accepts_real_bgm_file():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "bgm": {"file": "upbeat_01.mp3", "gain_db": -10}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["bgm"]["file"] == "upbeat_01.mp3"
    assert normalized["bgm"]["gain_db"] == -10.0
    assert normalized["bgm"]["ducking"] is True


def test_validate_plan_accepts_ducking_false():
    project = _make_project_with_clip()
    plan = {
        "shots": [_base_shot(project)], "narration_text": "",
        "bgm": {"file": "upbeat_01.mp3", "gain_db": -10, "ducking": False},
    }
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["bgm"]["ducking"] is False


def test_validate_plan_rejects_unknown_sfx_file():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "sfx": [{"file": "no_such_sfx.wav", "at_sec": 1.0}]}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("plan.sfx[0].file" in e for e in errors)


def test_validate_plan_accepts_real_sfx_files():
    project = _make_project_with_clip()
    plan = {
        "shots": [_base_shot(project)], "narration_text": "",
        "sfx": [{"file": "whoosh_01.wav", "at_sec": 1.0, "gain_db": -6}, {"file": "ding_01.wav", "at_sec": 2.0}],
    }
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert len(normalized["sfx"]) == 2


def test_validate_plan_rejects_invalid_subtitle_style_position():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "subtitle_style": {"position": "diagonal"}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("position" in e for e in errors)


def test_validate_plan_accepts_center_position():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "subtitle_style": {"position": "center"}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["subtitle_style"]["position"] == "center"


def test_validate_plan_rejects_invalid_accent_color_format():
    project = _make_project_with_clip()
    plan = {"shots": [_base_shot(project)], "narration_text": "", "subtitle_style": {"accent_color": "yellow"}}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("accent_color" in e for e in errors)


def test_resolve_clip_path_uses_projects_root_parent():
    project = _make_project_with_clip()
    clip_path = projects.media_relpath_for_clip(project["id"], "s1.mp4")
    resolved = projects.resolve_clip_path(project["id"], clip_path)
    assert resolved.exists()
    assert resolved == projects.clips_dir(project["id"]) / "s1.mp4"


# ---------------------------------------------------------------------------
# BUG-invalid_plan回帰: クリップ未生成ショット（clip_path=None）を含むplanの保存
# （生成途中失敗プロジェクトをStudioで保存できることの再現テスト）
# ---------------------------------------------------------------------------

def test_validate_plan_accepts_unrendered_shot_with_null_clip_path():
    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["clip_path"] = None
    plan = {"shots": [shot], "narration_text": "こんにちは"}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["shots"][0]["clip_path"] is None


def test_validate_plan_accepts_unrendered_shot_with_empty_clip_path():
    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["clip_path"] = ""
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["shots"][0]["clip_path"] is None


# ---------------------------------------------------------------------------
# パストラバーサル対策回帰: resolve_media_relpath/resolve_clip_path が
# PROJECTS_ROOT配下から逸脱する解決結果を拒否すること
# ---------------------------------------------------------------------------

def test_resolve_media_relpath_rejects_parent_traversal():
    with pytest.raises(projects.UnsafeMediaPathError):
        projects.resolve_media_relpath("../../../../etc/passwd")


def test_resolve_media_relpath_rejects_traversal_embedded_mid_path(tmp_path):
    # 一見 "projects/<id>/clips/..." から始まっていても、途中の "../" でPROJECTS_ROOT外へ
    # 抜け出すパターンも拒否されること。
    outside_target = str(projects.PROJECTS_ROOT.parent.parent / "secret.txt")
    traversal = "projects/p_x/clips/../../../{}".format(Path(outside_target).name)
    with pytest.raises(projects.UnsafeMediaPathError):
        projects.resolve_media_relpath(traversal)


def test_resolve_media_relpath_rejects_absolute_path_outside_root(tmp_path):
    outside_abs = str(tmp_path / "outside" / "evil.mp4")
    with pytest.raises(projects.UnsafeMediaPathError):
        projects.resolve_media_relpath(outside_abs)


def test_resolve_clip_path_rejects_traversal():
    with pytest.raises(projects.UnsafeMediaPathError):
        projects.resolve_clip_path("p_any", "../../outside.mp4")


def test_resolve_media_relpath_accepts_normal_relpath_within_root():
    # 既存の正常系（従来どおり通ること）の非退行確認。
    project = _make_project_with_clip()
    clip_path = projects.media_relpath_for_clip(project["id"], "s1.mp4")
    resolved = projects.resolve_media_relpath(clip_path)
    assert resolved.exists()
    assert resolved == projects.clips_dir(project["id"]) / "s1.mp4"


def test_validate_plan_rejects_clip_path_with_parent_traversal():
    project = _make_project_with_clip()
    shot = _base_shot(project, clip_path="../../../../etc/passwd")
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("clip_path" in e and "不正" in e for e in errors)
    assert normalized is None


def test_validate_plan_still_rejects_nonexistent_clip_file_when_clip_path_present():
    project = _make_project_with_clip()
    shot = _base_shot(project, clip_path=projects.media_relpath_for_clip(project["id"], "does_not_exist.mp4"))
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("存在しません" in e for e in errors)


def test_validate_plan_still_validates_trim_and_duration_for_unrendered_shot():
    project = _make_project_with_clip()
    shot = _base_shot(project, start=0.0, end=999.0)
    shot["clip_path"] = None
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert not ok
    assert any("trim範囲" in e for e in errors)


def test_unrendered_enabled_shot_ids_lists_enabled_shots_without_clip():
    project = _make_project_with_clip()
    s1 = _base_shot(project)
    s1["id"], s1["order"] = "s1", 0
    s2 = _base_shot(project)
    s2["id"], s2["order"], s2["clip_path"] = "s2", 1, None
    s3 = _base_shot(project)
    s3["id"], s3["order"], s3["clip_path"], s3["enabled"] = "s3", 2, None, False
    plan = {"shots": [s1, s2, s3], "narration_text": ""}
    ids = projects.unrendered_enabled_shot_ids(plan)
    assert ids == ["s2"]


# ---------------------------------------------------------------------------
# 商品アフィリエイト動画モード統合: product/tts永続化・image_pathパススルー・
# 薬機法ng_patternsの合成（productモードのみ有効化される想定）
# ---------------------------------------------------------------------------

def test_create_project_initializes_product_and_tts_to_none():
    project = projects.create_project("商品モードテスト", 15.0, "mock", status="draft", product_url="https://shop.example.com/item/1")
    assert project["product"] is None
    assert project["tts"] is None
    loaded = projects.get_project(project["id"])
    assert loaded["product"] is None
    assert loaded["tts"] is None


def test_get_project_setdefaults_product_and_tts_for_legacy_project_json():
    """本機能追加前に作られたproject.json（product/ttsキーが無い）を読んでも安全に扱えること。"""
    project = _make_project_with_clip()
    raw = projects.get_project(project["id"])
    del raw["product"]
    del raw["tts"]
    projects._project_json_path(project["id"]).write_text(
        __import__("json").dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    reloaded = projects.get_project(project["id"])
    assert reloaded["product"] is None
    assert reloaded["tts"] is None


def test_validate_plan_passes_through_string_image_path():
    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["image_path"] = "/tmp/fake_product_image_001.jpg"
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert normalized["shots"][0]["image_path"] == "/tmp/fake_product_image_001.jpg"


def test_validate_plan_omits_image_path_key_when_absent():
    project = _make_project_with_clip()
    shot = _base_shot(project)
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan)
    assert ok, errors
    assert "image_path" not in normalized["shots"][0]


def test_validate_plan_ng_patterns_none_by_default_allows_yakkiho_word():
    """通常モード(ng_patterns未指定)では薬機法ワードは検査されない(既定NGワードのみ)。"""
    from pipeline import compliance

    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["caption"] = "このシミが消える美容液"  # BEAUTY_YAKKIHO_NG_WORDS に含まれる文言
    plan = {"shots": [shot], "narration_text": ""}
    ok, errors, normalized = projects.validate_plan(project["id"], plan, ng_words=list(compliance.DEFAULT_NG_WORDS))
    assert ok, errors  # 薬機法ワードはDEFAULT_NG_WORDSに含まれないため通常モードでは弾かれない


def test_validate_plan_ng_patterns_blocks_yakkiho_word_when_product_mode_words_supplied():
    """商品モード相当（呼び出し側がBEAUTY_YAKKIHO_NG_WORDS/NG_PATTERNSを合成して渡す）では弾かれる。"""
    from pipeline import compliance

    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["caption"] = "このシミが消える美容液"
    plan = {"shots": [shot], "narration_text": ""}
    ng_words = list(compliance.DEFAULT_NG_WORDS) + list(compliance.BEAUTY_YAKKIHO_NG_WORDS)
    ok, errors, normalized = projects.validate_plan(
        project["id"], plan, ng_words=ng_words, ng_patterns=compliance.BEAUTY_YAKKIHO_NG_PATTERNS
    )
    assert not ok
    assert any("コンプライアンス違反" in e for e in errors)


def test_validate_plan_ng_patterns_catches_loose_yakkiho_phrasing_not_in_word_list():
    """固定文言リストに無い言い回し（例:「頑固なシミがみるみる消えます」）はng_patternsが補足する。"""
    from pipeline import compliance

    project = _make_project_with_clip()
    shot = _base_shot(project)
    shot["caption"] = "頑固なシミがみるみる消えます"
    plan = {"shots": [shot], "narration_text": ""}
    ng_words = list(compliance.DEFAULT_NG_WORDS) + list(compliance.BEAUTY_YAKKIHO_NG_WORDS)
    ok, errors, normalized = projects.validate_plan(
        project["id"], plan, ng_words=ng_words, ng_patterns=compliance.BEAUTY_YAKKIHO_NG_PATTERNS
    )
    assert not ok
    assert any("コンプライアンス違反" in e for e in errors)
