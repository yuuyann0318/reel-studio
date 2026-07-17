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


# ---------------------------------------------------------------------------
# TOCTOU回帰: PUT /plan がリクエスト受信時点の古いstatusスナップショットのまま
# 保存し、間に割り込んだ POST /render の "rendering" 遷移を巻き戻してしまわないこと
# ---------------------------------------------------------------------------

def test_put_plan_does_not_clobber_status_changed_during_validation(monkeypatch):
    """update_planがGET時点のproject（status="draft"）を保持したまま、
    validate_plan実行中（重い処理を模擬）に別スレッド相当のPOST /renderがstatusを
    "rendering"へ遷移させた場合、PUT /planの保存がそのrendering状態を"draft"へ
    巻き戻さないこと（=保存直前の再読込・再判定で409を返し保存自体を行わない）。
    """
    project = _make_project_with_one_enabled_shot("TOCTOUガード:PUT plan中の並行render")
    assert project["status"] == "draft"

    original_validate_plan = projects.validate_plan

    def _validate_plan_with_concurrent_render(project_id, plan, **kwargs):
        # PUT /plan のリクエスト処理中（validate_plan実行中）に、別スレッドの
        # POST /render が割り込んでstatusをrenderingへ遷移させたことを模擬する。
        concurrent = projects.get_project(project_id)
        concurrent["status"] = "rendering"
        projects.save_project(concurrent)
        return original_validate_plan(project_id, plan, **kwargs)

    monkeypatch.setattr(projects, "validate_plan", _validate_plan_with_concurrent_render)

    plan = dict(project["plan"])
    plan["narration_text"] = "並行編集による上書きテスト（保存されてはいけない）"

    resp = client.put("/api/projects/{}/plan".format(project["id"]), json=plan)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"

    saved = projects.get_project(project["id"])
    assert saved["status"] == "rendering"
    assert saved["plan"]["narration_text"] != "並行編集による上書きテスト（保存されてはいけない）"


def test_try_update_plan_still_saves_when_status_unchanged_during_validation(monkeypatch):
    """非退行確認: 保存直前の再読込時点でもstatusがdraft/readyのままなら、
    従来どおり正常に保存されること（アトミック化で通常経路を壊していないこと）。"""
    project = _make_project_with_one_enabled_shot("TOCTOUガード:通常経路は不変")
    plan = dict(project["plan"])
    plan["narration_text"] = "通常どおり保存されるはずのナレーション"

    resp = client.put("/api/projects/{}/plan".format(project["id"]), json=plan)
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["narration_text"] == "通常どおり保存されるはずのナレーション"

    saved = projects.get_project(project["id"])
    assert saved["plan"]["narration_text"] == "通常どおり保存されるはずのナレーション"


# ---------------------------------------------------------------------------
# 二重enqueue防止回帰: try_start_render の状態確認+遷移+enqueueがアトミックであり、
# 直後の2回目呼び出しは RenderConflictError になること
# ---------------------------------------------------------------------------

def test_try_start_render_rejects_immediately_repeated_call(monkeypatch):
    project = _make_project_with_one_enabled_shot("二重enqueue防止テスト")
    manager = _make_job_manager_without_worker(monkeypatch)

    job_id_1 = manager.try_start_render(project["id"])
    assert job_id_1 is not None

    with pytest.raises(jobs_mod.RenderConflictError):
        manager.try_start_render(project["id"])

    saved = projects.get_project(project["id"])
    assert saved["status"] == "rendering"
