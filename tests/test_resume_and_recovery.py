# -*- coding: utf-8 -*-
"""失敗原因の保全（error_history）・サーバ起動時のスタック回復・続きから生成(resume)・
台本メタ(director)永続化の単体テスト。

背景（実機で確認済みの不具合）: 14ショット中s1〜s10生成後に生成が中断し、s11〜s14が
clip_path=nullのまま。その後のrender要求で「編集内容の検証に失敗しました…」が
project["error"]を上書きし、元の失敗原因が消失した。さらに途中まで生成済み
（クレジット消費済み）なのに続きから再開する手段がUIにもAPIにも無かった。

本テストは以下をカバーする:
  (a) fail時のerror_history蓄積（上書きされず追記される）
  (b) 起動時スタック回復（generating/rendering のまま残ったプロジェクトをfailedへ）
  (c) resume正常系（clip_path未生成のショットだけ生成→全部揃ったらready）
  (d) resume中（generating/rendering）への二重resumeは409
  (e) directorメタ（model_used/source/quality）の永続化

ffmpeg/TTS/実際のレンダリングは実行しない（既存test_jobs_generate.pyと同じ手法で
render.run_ffmpeg・_render_projectをmonkeypatchする）。実Higgsfield生成は行わない。
"""
import shutil

import pytest
from fastapi.testclient import TestClient

from studio.server import jobs as jobs_mod
from studio.server import projects
from studio.server.app import app, job_manager

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_job_manager_without_worker(monkeypatch):
    """バックグラウンドワーカースレッドを起動せず、_run_*を同期的に呼べるJobManagerを作る
    （tests/test_jobs_generate.py の _make_job_manager_without_worker と同じ手法）。
    """
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


class _AlwaysFailingBackend:
    name = "mock"

    def __init__(self, cfg=None):
        pass

    def generate(self, shot, out_path):
        raise RuntimeError("simulated visual backend failure")


class _SucceedingBackend:
    """呼ばれたshot_idを記録しつつ、常に成功するビジュアルバックエンド。"""

    name = "mock"

    def __init__(self, cfg=None):
        self.calls = []

    def generate(self, shot, out_path):
        self.calls.append(shot.get("id"))
        with open(out_path, "wb") as f:
            f.write(b"\x00")
        return {"backend": self.name}


def _fake_run_director_with_meta(meta):
    def _fake(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        from pipeline import plan_schema
        plan = plan_schema.build_rule_based_plan(theme, target_duration_sec=target_duration_sec or 15, shot_count=2)
        plan["meta"] = dict(meta)
        return plan
    return _fake


# ---------------------------------------------------------------------------
# (a) error_history 蓄積（上書きされず追記される）
# ---------------------------------------------------------------------------

def test_fail_appends_to_error_history_without_losing_earlier_entries(monkeypatch):
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_with_meta({"source": "ai", "model_used": "m1"}))
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _AlwaysFailingBackend())

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("エラー履歴テスト", 10.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "エラー履歴テスト",
            "target_duration_sec": 10.0, "backend_name": "mock",
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert len(saved["error_history"]) == 1
        assert saved["error_history"][0]["kind"] == "generate"
        assert saved["error_history"][0]["message"] == saved["error"]
        assert saved["error_history"][0]["ts"]

        # 2回目の失敗（render）: project["error"]は最新に更新されるが、error_historyは追記のみ
        saved["status"] = "draft"
        saved["plan"]["shots"] = [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "p", "caption": "c",
            "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }]
        projects.save_project(saved)

        manager._run_render(saved["id"], {"project_id": saved["id"]})
        saved2 = projects.get_project(project["id"])
        assert saved2["status"] == "failed"
        assert len(saved2["error_history"]) == 2
        assert saved2["error_history"][0]["kind"] == "generate"  # 1回目の原因が消えていない
        assert saved2["error_history"][1]["kind"] == "render"
        assert saved2["error"] == saved2["error_history"][1]["message"]  # error自体は最新
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (b) 起動時スタック回復
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stuck_status,expected_kind", [("generating", "generate"), ("rendering", "render")])
def test_recover_stuck_projects_on_startup(monkeypatch, stuck_status, expected_kind):
    project = projects.create_project("スタック回復テスト", 10.0, "mock", status=stuck_status)
    try:
        manager = _make_job_manager_without_worker(monkeypatch)  # __init__内で回復処理が走る
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "再起動" in saved["error"]
        assert len(saved["error_history"]) == 1
        assert saved["error_history"][0]["kind"] == expected_kind
        assert manager is not None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_recover_stuck_projects_leaves_ready_and_failed_projects_untouched(monkeypatch):
    ready = projects.create_project("回復対象外:ready", 10.0, "mock", status="ready")
    failed = projects.create_project("回復対象外:failed", 10.0, "mock", status="failed")
    try:
        _make_job_manager_without_worker(monkeypatch)
        assert projects.get_project(ready["id"])["status"] == "ready"
        saved_failed = projects.get_project(failed["id"])
        assert saved_failed["status"] == "failed"
        assert saved_failed["error_history"] == []  # 元々失敗していたプロジェクトの履歴は増えない
    finally:
        shutil.rmtree(projects.project_dir(ready["id"]), ignore_errors=True)
        shutil.rmtree(projects.project_dir(failed["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (c) resume正常系: 欠損ショットだけ生成→ready
# ---------------------------------------------------------------------------

def test_run_resume_generates_only_missing_shots_then_ready(monkeypatch):
    project = projects.create_project("resume正常系テスト", 10.0, "mock", status="failed")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "s1.mp4").write_bytes(b"\x00")

    project["error"] = "s2/s3の生成に失敗しました"
    project["plan"] = {
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "prompt": "p1", "caption": "c1",
             "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
             "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
            {"id": "s2", "order": 1, "enabled": True, "prompt": "p2", "caption": "c2",
             "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
            {"id": "s3", "order": 2, "enabled": False, "prompt": "p3", "caption": "c3",
             "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
            {"id": "s4", "order": 3, "enabled": True, "prompt": "p4", "caption": "c4",
             "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
        ],
        "narration_text": "テストナレーション",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    backend = _SucceedingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    captured_render_plan = {}

    def _fake_render_project(project_id, plan, cfg):
        captured_render_plan["plan"] = plan
        return projects.media_relpath_for_render(project_id, "out.mp4"), 15.0, {"backend": "say", "duration_sec": 15.0, "is_silent": False}

    monkeypatch.setattr(jobs_mod, "_render_project", _fake_render_project)

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_resume_test", {"project_id": project["id"]})

        # enabled かつ 未生成(s2,s4)だけが生成対象。s1(既生成)・s3(無効)は呼ばれない
        assert sorted(backend.calls) == ["s2", "s4"]

        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert len(saved["renders"]) == 1
        assert saved["renders"][0]["path"] == projects.media_relpath_for_render(project["id"], "out.mp4")

        shots_by_id = {s["id"]: s for s in saved["plan"]["shots"]}
        assert shots_by_id["s1"]["clip_path"] is not None  # 既存クリップは維持
        assert shots_by_id["s2"]["clip_path"] == projects.media_relpath_for_clip(project["id"], "s2.mp4")
        assert shots_by_id["s4"]["clip_path"] == projects.media_relpath_for_clip(project["id"], "s4.mp4")
        assert shots_by_id["s3"]["clip_path"] is None  # 無効ショットは生成されない

        # _render_projectへ渡されたplanでは、有効ショットのclip_pathが全て埋まっている
        rendered_plan = captured_render_plan["plan"]
        for s in rendered_plan["shots"]:
            if s.get("enabled", True):
                assert s["clip_path"]
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_resume_records_progress_up_to_failure_point(monkeypatch):
    """s2は成功・s4で失敗するケース: error_historyのmessageにここまでの成功ショットが含まれる。"""
    project = projects.create_project("resume途中失敗テスト", 10.0, "mock", status="failed")
    project["plan"] = {
        "shots": [
            {"id": "s2", "order": 0, "enabled": True, "prompt": "p2", "caption": "c2",
             "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
            {"id": "s4", "order": 1, "enabled": True, "prompt": "p4", "caption": "c4",
             "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
        ],
        "narration_text": "テスト", "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    class _FailsOnSecondBackend:
        name = "mock"

        def __init__(self, cfg=None):
            self.calls = []

        def generate(self, shot, out_path):
            self.calls.append(shot.get("id"))
            if len(self.calls) == 1:
                with open(out_path, "wb") as f:
                    f.write(b"\x00")
                return {"backend": self.name}
            raise RuntimeError("s4 backend failure")

    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _FailsOnSecondBackend())
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_resume_partial", {"project_id": project["id"]})

        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "s4" in saved["error"]
        assert "s2" in saved["error"]  # ここまで成功したショットがmessageに含まれる
        assert saved["error_history"][-1]["kind"] == "resume"

        shots_by_id = {s["id"]: s for s in saved["plan"]["shots"]}
        assert shots_by_id["s2"]["clip_path"] is not None
        assert shots_by_id["s4"]["clip_path"] is None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (d) resume中への二重resumeは409 / 存在しなければ404
# ---------------------------------------------------------------------------

def test_resume_endpoint_returns_409_while_status_is_rendering():
    project = projects.create_project("resume競合テスト", 10.0, "mock", status="rendering")
    try:
        resp = client.post("/api/projects/{}/resume".format(project["id"]))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
        # 拒否されたのでstatusは変わっていない
        assert projects.get_project(project["id"])["status"] == "rendering"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_endpoint_returns_409_while_status_is_generating():
    project = projects.create_project("resume競合テスト2", 10.0, "mock", status="generating")
    try:
        resp = client.post("/api/projects/{}/resume".format(project["id"]))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_endpoint_returns_404_for_unknown_project():
    resp = client.post("/api/projects/p_does_not_exist_xyz/resume")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_try_start_resume_transitions_status_to_generating_and_returns_job_id(monkeypatch):
    """job_manager.try_start_resume自体の受理経路（statusのアトミック遷移+job_id発行）を確認する。

    実ワーカースレッドを起動しないマネージャーを使う（_make_job_manager_without_worker）。
    実singletonで検証すると、enqueueされたジョブを実ワーカーが並行して処理してしまい、
    アサーション対象のstatusが検証前に書き換わるレースになるため。
    """
    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("try_start_resumeテスト", 10.0, "mock", status="failed")
    try:
        job_id = manager.try_start_resume(project["id"])
        assert job_id
        saved = projects.get_project(project["id"])
        assert saved["status"] == "generating"

        with pytest.raises(jobs_mod.RenderConflictError):
            manager.try_start_resume(project["id"])
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_unknown_project_returns_none_directly():
    assert job_manager.try_start_resume("p_does_not_exist_xyz") is None


# ---------------------------------------------------------------------------
# (e) directorメタ（model_used/source/quality）の永続化
# ---------------------------------------------------------------------------

def test_run_generate_persists_director_meta_for_ai_source(monkeypatch):
    monkeypatch.setattr(
        jobs_mod.director, "run_director",
        _fake_run_director_with_meta({"source": "ai", "model_used": "claude-sonnet-5", "quality": "high"}),
    )
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _AlwaysFailingBackend())

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("directorメタ:ai", 10.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "directorメタ:ai",
            "target_duration_sec": 10.0, "backend_name": "mock",
        })
        saved = projects.get_project(project["id"])
        # ビジュアル生成自体は失敗しても、director企画生成は成功しているのでメタは保存される
        assert saved["director"] == {"model_used": "claude-sonnet-5", "source": "ai", "quality": "high"}
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_persists_director_meta_for_rule_based_fallback(monkeypatch):
    monkeypatch.setattr(
        jobs_mod.director, "run_director",
        _fake_run_director_with_meta({"source": "rule", "model_used": None}),
    )
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _AlwaysFailingBackend())

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("directorメタ:rule", 10.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "directorメタ:rule",
            "target_duration_sec": 10.0, "backend_name": "mock",
        })
        saved = projects.get_project(project["id"])
        assert saved["director"]["source"] == "rule"
        assert saved["director"]["model_used"] is None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_get_project_response_includes_unrendered_shot_ids_and_backfills_old_fields():
    """GET /api/projects/{id}: 既存フィールドを壊さず、error_history/director/unrendered_shot_idsを含む。"""
    project = projects.create_project("GET応答拡張テスト", 10.0, "mock", status="draft")
    project["plan"]["shots"] = [
        {"id": "s1", "order": 0, "enabled": True, "prompt": "p", "caption": "c",
         "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0}},
    ]
    # 旧project.json互換: error_history/directorキーが無い状態を模倣する
    del project["error_history"]
    del project["director"]
    projects.save_project(project)

    try:
        resp = client.get("/api/projects/{}".format(project["id"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["unrendered_shot_ids"] == ["s1"]
        assert body["error_history"] == []
        assert body["director"] is None
        assert body["theme"] == "GET応答拡張テスト"  # 既存フィールドは維持される
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_endpoint_returns_409_when_status_is_ready():
    """レビュー指摘の回帰テスト: failed以外(ready等)へのresumeは拒否する。

    readyなプロジェクトにresumeが通ると、不要な全体再レンダリングと
    renders履歴の重複追加が起きるため、APIレベルで明確に拒否する。
    """
    project = projects.create_project("resume不要テスト", 10.0, "mock", status="ready")
    try:
        resp = client.post("/api/projects/{}/resume".format(project["id"]))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
        # 拒否されたのでstatusはreadyのまま（generatingに書き換えられていない）
        assert projects.get_project(project["id"])["status"] == "ready"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)
