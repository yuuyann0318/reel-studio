# -*- coding: utf-8 -*-
"""studio/server/projects.py の単体テスト（永続化・plan検証）。

PROJECTS_ROOTをtmp_pathへ差し替えて実プロジェクトディレクトリを汚さない。
"""
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
