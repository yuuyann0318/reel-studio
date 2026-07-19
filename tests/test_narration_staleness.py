# -*- coding: utf-8 -*-
"""narration_segments陳腐化ガードの回帰テスト。

独立レビュー指摘: PUT /api/projects/{id}/plan (job_manager.try_update_plan) が
narration_text（全文ナレーション）や有効ショットのtrimを書き換えても、
project["narration_segments"]（音声主導タイミング同期モード用のショット別音声断片テキスト）を
そのまま残していた。これだと、
  - narration_textを書き換えても古いnarration_segmentsで同期モードの音声が組まれ、
    テキスト変更が音声に反映されない（古い音声の残留）。
  - trimを書き換えても古い断片尺のまま境界がずれる（ユーザーのtrim編集意図の無視）。

修正: narration_text or 有効ショットのtrimが変わった場合、保存直前に
narration_segmentsをクリアし、次回レンダリングを全文方式(full)へ自動フォールバックさせる。
"""
import pytest

from studio.server import jobs as jobs_mod
from studio.server import projects


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_project_with_segments(theme):
    project = projects.create_project(theme, 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "s1.mp4"
    clip_path.write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract", "caption": "テスト",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }],
        "narration_text": "元のナレーションです。",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    project["narration_segments"] = {"s1": "元のナレーションです。"}
    project["tts"] = {"backend": "fake", "duration_sec": 5.0, "is_silent": False, "mode": "segment"}
    projects.save_project(project)
    return project


def _job_manager(monkeypatch):
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def test_try_update_plan_clears_narration_segments_when_narration_text_changes(monkeypatch):
    project = _make_project_with_segments("陳腐化テスト:ナレーション編集")
    manager = _job_manager(monkeypatch)

    plan = dict(project["plan"])
    plan["narration_text"] = "編集後の新しいナレーションです。"

    updated = manager.try_update_plan(project["id"], plan)

    assert updated["narration_segments"] == {}
    saved = projects.get_project(project["id"])
    assert saved["narration_segments"] == {}
    # tts.modeの直近実績表示自体は書き換えない（次のレンダリングまでは直近の実績のまま）。
    assert saved["tts"]["mode"] == "segment"


def test_try_update_plan_clears_narration_segments_when_enabled_shot_trim_changes(monkeypatch):
    project = _make_project_with_segments("陳腐化テスト:trim編集")
    manager = _job_manager(monkeypatch)

    plan = {
        "shots": [dict(project["plan"]["shots"][0], trim={"start": 0.5, "end": 4.0})],
        "narration_text": project["plan"]["narration_text"],
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }

    updated = manager.try_update_plan(project["id"], plan)

    assert updated["narration_segments"] == {}


def test_try_update_plan_keeps_narration_segments_when_disabled_shot_trim_changes(monkeypatch):
    """無効(enabled=False)ショットのtrimが変わっても同期モードの音声には影響しないため、
    narration_segmentsはクリアしない（誤って全文モードへ落とさない）。"""
    project = _make_project_with_segments("陳腐化テスト:無効ショットtrim変更")
    manager = _job_manager(monkeypatch)
    project = projects.get_project(project["id"])
    project["plan"]["shots"][0]["enabled"] = False
    projects.save_project(project)

    plan = {
        "shots": [dict(project["plan"]["shots"][0], trim={"start": 1.0, "end": 3.0})],
        "narration_text": project["plan"]["narration_text"],
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }

    updated = manager.try_update_plan(project["id"], plan)

    assert updated["narration_segments"] == {"s1": "元のナレーションです。"}


def test_try_update_plan_keeps_narration_segments_when_nothing_relevant_changes(monkeypatch):
    project = _make_project_with_segments("陳腐化テスト:変更なし")
    manager = _job_manager(monkeypatch)

    plan = dict(project["plan"])
    # subtitle_styleだけ変える（narration_text/trimは不変）。
    plan["subtitle_style"] = dict(plan["subtitle_style"], font_size=90)

    updated = manager.try_update_plan(project["id"], plan)

    assert updated["narration_segments"] == {"s1": "元のナレーションです。"}


def test_try_update_plan_no_narration_segments_key_is_unaffected(monkeypatch):
    """narration_segmentsキー自体が無い(旧)プロジェクトの編集でも例外にならず、
    引き続きキーが存在しない（=既定の全文方式）まま保存できること。"""
    project = projects.create_project("陳腐化テスト:キー無し", 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "s1.mp4"
    clip_path.write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract", "caption": "テスト",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }],
        "narration_text": "全文ナレーション。",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)
    assert "narration_segments" not in project

    manager = _job_manager(monkeypatch)
    plan = dict(project["plan"])
    plan["narration_text"] = "書き換え後の全文ナレーション。"

    updated = manager.try_update_plan(project["id"], plan)

    assert updated.get("narration_segments") in (None, {})
