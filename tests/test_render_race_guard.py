# -*- coding: utf-8 -*-
"""render受理〜ワーカー実行間のレース回帰テスト。

指摘: validate_plan は clip_path=None を許容するため、try_start_render() が
unrendered_enabled_shot_ids() で有効ショットの未生成をチェックして受理した直後に、
PUT /plan で同じ有効ショットの clip_path が null に書き換えられると、
_run_render -> _render_project -> resolve_clip_path(None) が TypeError
（Path(None)）を投げ、broad except に握られて素の例外文言がユーザーに返ってしまう。

対策:
  1. jobs.JobManager._run_render 内でも unrendered_enabled_shot_ids() を再チェックし、
     素のTypeErrorではなく明確な失敗メッセージでジョブをfailさせる。
  2. app.update_plan (PUT /plan) は project.status が generating/rendering の間、
     編集そのものを409で拒否する（レースの発生源を塞ぐ）。

このファイルは2つのレイヤーを別々にテストする:
  - test_run_render_* : jobs.JobManager._run_render を直接呼び、レース状態
    （project.jsonのplanが直接書き換えられ、clip_path=Noneの有効ショットが
    残っている状態）を再現し、TypeErrorが漏れずに明確な失敗になることを確認する。
  - test_put_plan_* : FastAPI TestClient経由でPUT /planのステータスガードを確認する。
"""
import shutil

import pytest
from fastapi.testclient import TestClient

from studio.server import jobs as jobs_mod
from studio.server import projects
from studio.server.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_job_manager_without_worker(monkeypatch):
    """バックグラウンドワーカースレッドを起動せず、_run_renderを同期的に呼べるJobManagerを作る
    （tests/test_jobs_generate.py の _make_job_manager_without_worker と同じ手法）。
    """
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def _make_project_with_one_enabled_shot(theme):
    project = projects.create_project(theme, 5.0, "mock", status="draft")
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
        "narration_text": "テストナレーション",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)
    return project


def test_run_render_fails_cleanly_without_raising_when_enabled_shot_clip_path_becomes_null(monkeypatch):
    """レース再現: try_start_render受理相当の状態を作った直後、ワーカー実行前に
    有効ショットのclip_pathがnullへ書き換わっても、_run_renderが素のTypeErrorを
    投げず、明確な失敗メッセージでジョブをfailさせることを確認する。
    """
    project = _make_project_with_one_enabled_shot("レースガードテスト:_run_render")

    manager = _make_job_manager_without_worker(monkeypatch)

    # try_start_render() のunrendered_enabled_shot_idsチェック通過相当（この時点ではclip_path実在）
    assert projects.unrendered_enabled_shot_ids(project["plan"]) == []

    # レース: ワーカー実行直前に、PUT /plan相当で有効ショットのclip_pathをnullへ書き換える
    project = projects.get_project(project["id"])
    project["plan"]["shots"][0]["clip_path"] = None
    project["status"] = "rendering"
    projects.save_project(project)

    job_id = "job_race_test_001"
    # _run_render自体が例外を投げないこと（TypeErrorが握りつぶされずにここで再発しないこと）を保証する
    manager._run_render(job_id, {"project_id": project["id"]})

    saved = projects.get_project(project["id"])
    assert saved["status"] == "failed"
    assert "s1" in (saved.get("error") or "")
    assert "未生成" in (saved.get("error") or "")

    snapshot = manager.get_snapshot(job_id)
    assert snapshot["done"] is True
    assert snapshot["ok"] is False
    # 素のTypeError/Noneのstr表現がユーザー向けメッセージに漏れていないこと
    assert "NoneType" not in snapshot["message"]
    assert "TypeError" not in snapshot["message"]


def test_put_plan_rejects_edit_while_status_is_rendering_with_409():
    project = _make_project_with_one_enabled_shot("レースガードテスト:PUT plan中rendering拒否")
    project["status"] = "rendering"
    projects.save_project(project)

    resp = client.put("/api/projects/{}/plan".format(project["id"]), json=project["plan"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"

    # 拒否されたのでplanは書き換わっていないこと
    reloaded = projects.get_project(project["id"])
    assert reloaded["status"] == "rendering"


def test_put_plan_rejects_edit_while_status_is_generating_with_409():
    project = _make_project_with_one_enabled_shot("レースガードテスト:PUT plan中generating拒否")
    project["status"] = "generating"
    projects.save_project(project)

    resp = client.put("/api/projects/{}/plan".format(project["id"]), json=project["plan"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_put_plan_still_allowed_while_status_is_draft():
    """既存挙動の非退行確認: rendering/generating以外（draft）は従来どおり編集できること。"""
    project = _make_project_with_one_enabled_shot("レースガードテスト:draftは許可")
    plan = dict(project["plan"])
    plan["narration_text"] = "編集後のナレーション"

    resp = client.put("/api/projects/{}/plan".format(project["id"]), json=plan)
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["narration_text"] == "編集後のナレーション"
