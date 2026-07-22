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
        # T11: build_rule_based_plan -> build_smoke_plan (TTP v2 Phase 2)。
        plan = plan_schema.build_smoke_plan(theme, target_duration_sec=target_duration_sec or 15, shot_count=2)
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
        # BUG-21修正: JobManager() 生成だけでは回復は走らない（サーバ起動イベント経由でのみ
        # 明示的に呼ぶ設計になったため、ここでも明示的に呼ぶ）。
        manager = _make_job_manager_without_worker(monkeypatch)
        manager.recover_stuck_projects()
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
        manager = _make_job_manager_without_worker(monkeypatch)
        manager.recover_stuck_projects()
        assert projects.get_project(ready["id"])["status"] == "ready"
        saved_failed = projects.get_project(failed["id"])
        assert saved_failed["status"] == "failed"
        assert saved_failed["error_history"] == []  # 元々失敗していたプロジェクトの履歴は増えない
    finally:
        shutil.rmtree(projects.project_dir(ready["id"]), ignore_errors=True)
        shutil.rmtree(projects.project_dir(failed["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (b') BUG-21回帰テスト: import副作用の除去 + startup限定での回復
#
# 実害: JobManager.__init__ が無条件に _recover_stuck_projects() を呼んでいたため、
# studio.server.jobs を import しただけの別プロセス（pytest収集等）が本物のprojects/を
# 走査し、稼働中(generating/rendering)のプロジェクトを勝手にfailedへ倒してしまっていた。
# ---------------------------------------------------------------------------

def test_job_manager_construction_alone_does_not_touch_project_status(monkeypatch):
    """JobManager() を生成しただけ（起動イベント発火前）では、稼働中プロジェクトのstatusは
    書き換わらないこと（importするだけでスタック回復が走っていた旧挙動の回帰テスト）。
    """
    project = projects.create_project("import副作用回帰テスト", 10.0, "mock", status="generating")
    try:
        _make_job_manager_without_worker(monkeypatch)  # 生成のみ。recover_stuck_projects()は呼ばない
        saved = projects.get_project(project["id"])
        assert saved["status"] == "generating"  # 書き換わっていない
        assert saved["error_history"] == []
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_testclient_startup_triggers_recovery(monkeypatch):
    """FastAPIのlifespan(=実サーバプロセス起動時に相当)発火時にのみ、スタック回復が走ること。

    `with TestClient(app) as c:` で ASGI lifespan の startup/shutdown を明示的に発火させる
    （モジュール直下の `client = TestClient(app)`（このファイル冒頭）はcontext managerを
    使っていないためlifespanは発火せず、他のエンドポイントテストの挙動には影響しない）。
    """
    project = projects.create_project("startup回復テスト", 10.0, "mock", status="rendering")
    try:
        with TestClient(app) as c:
            resp = c.get("/api/projects/{}".format(project["id"]))
            assert resp.status_code == 200
            assert resp.json()["status"] == "failed"
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "再起動" in saved["error"]
        assert saved["error_history"][0]["kind"] == "render"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


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
# (c') シーングループ resume: マスター実在→再切り出しのみ(クレジット0) / 無し→シーン再生成 / 混在
# ---------------------------------------------------------------------------

def _fake_render_project_ok(project_id, plan, cfg):
    return projects.media_relpath_for_render(project_id, "out.mp4"), 6.0, {"backend": "say", "duration_sec": 6.0, "is_silent": False}


def _scene_project_with_pending(monkeypatch, master_on_disk):
    """sc1=(s1済/s2未) の failed プロジェクトを作る。master_on_disk=True でシーンマスターを実在させる。"""
    project = projects.create_project("シーンresume", 4.0, "mock", status="failed")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "s1.mp4").write_bytes(b"\x00")
    scene_master_rel = projects.media_relpath_for_clip(project["id"], "scene__sc1.mp4")
    if master_on_disk:
        (clips_dir / "scene__sc1.mp4").write_bytes(b"\x00")
    project["plan"] = {
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "prompt": "v1", "caption": "c1",
             "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
             "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}},
            {"id": "s2", "order": 1, "enabled": True, "prompt": "v1", "caption": "c2",
             "clip_path": None, "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}},
        ],
        "narration_text": "t", "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    project["scenes"] = [{
        "scene_key": "sc1", "shot_ids": ["s1", "s2"], "visual_prompt": "v1", "motion_preset": "zoom_in",
        "clip_path": scene_master_rel, "planned_duration_sec": 4.0, "actual_duration_sec": 4.0,
    }]
    projects.save_project(project)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda ffprobe_bin, path: 4.0)
    monkeypatch.setattr(jobs_mod, "_render_project", _fake_render_project_ok)
    return project


def test_resume_recut_only_when_scene_master_exists_no_credits(monkeypatch):
    project = _scene_project_with_pending(monkeypatch, master_on_disk=True)
    backend = _SucceedingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_scene_recut", {"project_id": project["id"]})
        # マスター実在 → backend は一度も呼ばれない（クレジット0）。再切り出しだけ
        assert backend.calls == []
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        shots_by_id = {s["id"]: s for s in saved["plan"]["shots"]}
        assert shots_by_id["s2"]["clip_path"] == projects.media_relpath_for_clip(project["id"], "s2.mp4")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_regenerates_scene_when_master_missing(monkeypatch):
    project = _scene_project_with_pending(monkeypatch, master_on_disk=False)
    backend = _SucceedingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_scene_regen", {"project_id": project["id"]})
        # マスター無し → シーン単位で1回だけ再生成（scene_key で呼ばれる）
        assert backend.calls == ["sc1"]
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        shots_by_id = {s["id"]: s for s in saved["plan"]["shots"]}
        assert shots_by_id["s2"]["clip_path"] is not None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_resume_mixes_scene_and_standalone_shots(monkeypatch):
    """sc1(マスター実在,s2未) + scenes非登録の単独ショット s3未 の混在。"""
    project = _scene_project_with_pending(monkeypatch, master_on_disk=True)
    saved = projects.get_project(project["id"])
    saved["plan"]["shots"].append(
        {"id": "s3", "order": 2, "enabled": True, "prompt": "v3", "caption": "c3",
         "clip_path": None, "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}}
    )
    projects.save_project(saved)

    backend = _SucceedingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_scene_mixed", {"project_id": project["id"]})
        # s2 はシーン再切り出し(生成呼び出し無し)、s3 は従来1ショット経路で生成される
        assert backend.calls == ["s3"]
        saved2 = projects.get_project(project["id"])
        assert saved2["status"] == "ready"
        shots_by_id = {s["id"]: s for s in saved2["plan"]["shots"]}
        assert shots_by_id["s2"]["clip_path"] is not None
        assert shots_by_id["s3"]["clip_path"] == projects.media_relpath_for_clip(project["id"], "s3.mp4")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (c'') resume時のシーン窓破壊回帰: 生成時の member_durations を尊重すること
# ---------------------------------------------------------------------------


def test_resume_uses_saved_member_durations_not_plan_shots(monkeypatch):
    """生成後にユーザーがショットを削除/trim編集しても、シーンマスターから切り出す窓は
    生成時と一致する（[高]バグ修正: シーン窓破壊）。

    シナリオ: sc1=[s1(1.0秒), s2(3.0秒), s3(4.0秒)] で生成。マスター実在。
    その後ユーザーがs1をplanから削除（PUT /plan相当）→ pending=[s2, s3]。
    scenes.member_durations=[1.0, 3.0, 4.0] が保存されていれば、s2の trim_start=1.0（正）。
    もし plan.shots の source_duration から作り直すと、s1が消えているため s2 の trim_start=0.0
    という誤った窓になり「別の絵の区間」を切り出してしまう。
    """
    project = projects.create_project("resume窓保護テスト", 8.0, "mock", status="failed")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    scene_master_rel = projects.media_relpath_for_clip(project["id"], "scene__sc1.mp4")
    (clips_dir / "scene__sc1.mp4").write_bytes(b"\x00")  # マスター実在

    # s1は削除済み。plan.shots に残っているのは s2, s3 のみ（両方 pending）。
    project["plan"] = {
        "shots": [
            {"id": "s2", "order": 0, "enabled": True, "prompt": "v1", "caption": "c2",
             "clip_path": None, "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0}},
            {"id": "s3", "order": 1, "enabled": True, "prompt": "v1", "caption": "c3",
             "clip_path": None, "source_duration": 4.0, "trim": {"start": 0.0, "end": 4.0}},
        ],
        "narration_text": "t", "bgm": None, "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    # scenes メタは生成時のスナップショットのまま（s1 も member_ids/member_durations に含まれる）。
    project["scenes"] = [{
        "scene_key": "sc1",
        "shot_ids": ["s1", "s2", "s3"],
        "member_durations": [1.0, 3.0, 4.0],
        "visual_prompt": "v1", "motion_preset": "static",
        "clip_path": scene_master_rel,
        "planned_duration_sec": 8.0, "actual_duration_sec": 8.0,
    }]
    projects.save_project(project)

    # 切り出し時のtrim_startを捕捉する。
    captured = []
    orig_build = jobs_mod.render.build_normalize_clip_cmd

    def _capture_build(ffmpeg_bin, src, dst, duration_sec=None, trim_start=0.0, **kwargs):
        # 出力ファイル名からshot_idを抜き出す（clips_dir / "s2.mp4" 等）。
        from pathlib import Path as _P
        sid = _P(dst).stem
        captured.append({"shot_id": sid, "trim_start": trim_start, "duration_sec": duration_sec})
        return orig_build(ffmpeg_bin, src, dst, duration_sec=duration_sec, trim_start=trim_start, **kwargs)

    monkeypatch.setattr(jobs_mod.render, "build_normalize_clip_cmd", _capture_build)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda ffprobe_bin, path: 8.0)
    monkeypatch.setattr(jobs_mod, "_render_project", _fake_render_project_ok)
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _SucceedingBackend())

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_window_preserved", {"project_id": project["id"]})
        # s2/s3 のtrim_start は 生成時のオフセット（累積尺）と一致する。
        trims = {c["shot_id"]: c["trim_start"] for c in captured if c["shot_id"] in ("s2", "s3")}
        assert trims.get("s2") == 1.0, "s2のtrim_startが生成時オフセットと不一致: {}".format(trims)
        assert trims.get("s3") == 4.0, "s3のtrim_startが生成時オフセットと不一致: {}".format(trims)
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
