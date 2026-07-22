# -*- coding: utf-8 -*-
"""studio/server/jobs.py JobManager._run_generate の単体テスト（BUG-10回帰防止）。

BUG-10: ビジュアル生成が（higgsfieldのduration制約等で）途中で失敗すると、
project.json の plan.shots が0件のまま（= directorが立てた企画が失われる）になっていた。
本来は失敗時も「何を計画していたか」（prompt/caption/尺）が project.json に残るべき。

PROJECTS_ROOTをtmp_pathへ差し替えて実プロジェクトディレクトリを汚さない。
claude CLI呼び出し（課金/低速）は director.run_director をルールベース生成に差し替えて回避する。
"""
import shutil

import pytest

from pipeline import plan_schema
from studio.server import jobs as jobs_mod
from studio.server import projects


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _fake_run_director_factory(shot_count):
    def _fake(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        # T9: build_rule_based_plan -> build_smoke_plan へ移行(TTP v2 Phase 2)。
        # BUG-10 の意図(ビジュアル失敗時も企画が失われないこと)は plan 中身に依存しないため、
        # 置換だけで既存テストは通る。
        return plan_schema.build_smoke_plan(
            theme, target_duration_sec=target_duration_sec or 15, shot_count=shot_count
        )
    return _fake


class _AlwaysFailingBackend:
    """全ショットの生成で必ず失敗するビジュアルバックエンド（初回ショットから失敗）。"""

    name = "mock"

    def __init__(self, cfg=None):
        pass

    def generate(self, shot, out_path):
        raise RuntimeError("simulated visual backend failure (BUG-10 repro)")


class _FailsAfterFirstShotBackend:
    """1本目のショットだけ成功し、2本目以降は失敗するビジュアルバックエンド。"""

    name = "mock"

    def __init__(self, cfg=None):
        self._count = 0

    def generate(self, shot, out_path):
        self._count += 1
        if self._count == 1:
            # 実際にファイルが要る（build_normalize_clip_cmd -> ffmpegに渡すため）。
            # ここでは正規化コマンド自体は実行されるので、有効なmp4である必要はあるが、
            # run_ffmpegの成否まではこのテストでは検証しないため、後段でffmpegごとmonkeypatchする。
            with open(out_path, "wb") as f:
                f.write(b"\x00")
            return {"backend": self.name}
        raise RuntimeError("simulated visual backend failure on shot #{}".format(self._count))


def _make_job_manager_without_worker(monkeypatch):
    """バックグラウンドワーカースレッドを起動せず、_run_generateを同期的に呼べるJobManagerを作る。"""
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def test_run_generate_preserves_plan_shots_when_first_shot_fails_bug10(monkeypatch):
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(3))
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _AlwaysFailingBackend())

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("BUG-10テスト:全滅", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"],
            "theme": "BUG-10テスト:全滅",
            "target_duration_sec": 15.0,
            "backend_name": "mock",
        })

        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert saved["error"]

        shots = saved["plan"]["shots"]
        # 修正前は shots が [] になっていた（=企画が失われていた）。修正後は3件保持される。
        assert len(shots) == 3
        assert all(s["clip_path"] is None for s in shots)
        assert all(s["prompt"] for s in shots)
        assert all(s["caption"] for s in shots)
        assert shots[0]["trim"]["end"] == pytest.approx(shots[0]["source_duration"])
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# シーングループ（クリップ再利用）: 生成はシーン数だけ・各ショットは切り出しで揃う
# ---------------------------------------------------------------------------

class _RecordingBackend:
    """generate呼び出しID（＝シーンkey or ショットid）と、その時点のmax_credits_per_shotを記録する。"""

    name = "mock"

    def __init__(self, cfg=None):
        self.generate_calls = []
        self.seen_credit_limits = []
        self.max_credits_per_shot = 30

    def generate(self, shot, out_path):
        self.generate_calls.append(shot.get("id"))
        self.seen_credit_limits.append(self.max_credits_per_shot)
        with open(out_path, "wb") as f:
            f.write(b"\x00")
        return {"backend": self.name}


def _fake_render_project(project_id, plan, cfg):
    return projects.media_relpath_for_render(project_id, "out.mp4"), 8.0, {"backend": "say", "duration_sec": 8.0, "is_silent": False}


def _scene_plan(shots):
    return {
        "version": 1,
        "meta": {"source": "ai", "model_used": "m"},
        "concept": "テスト企画", "hook": "テストフック",
        "narration_script": "テストナレーション本文です。",
        "shots": shots, "bgm_mood": "upbeat",
    }


def _setup_scene_generate(monkeypatch, shots):
    monkeypatch.setattr(jobs_mod.director, "run_director", lambda *a, **k: _scene_plan(shots))
    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda ffprobe_bin, path: 4.0)
    monkeypatch.setattr(jobs_mod, "_render_project", _fake_render_project)
    return backend


def test_run_generate_scene_grouping_generates_once_per_scene(monkeypatch):
    shots = [
        {"id": "s1", "scene_id": "sc1", "visual_prompt": "v1", "motion_preset": "zoom_in", "duration_sec": 2.0, "caption_jp": "c1"},
        {"id": "s2", "scene_id": "sc1", "visual_prompt": "v1", "motion_preset": "zoom_in", "duration_sec": 2.0, "caption_jp": "c2"},
        {"id": "s3", "scene_id": "sc2", "visual_prompt": "v3", "motion_preset": "pan_right", "duration_sec": 2.0, "caption_jp": "c3"},
        {"id": "s4", "scene_id": "sc2", "visual_prompt": "v3", "motion_preset": "pan_right", "duration_sec": 2.0, "caption_jp": "c4"},
    ]
    backend = _setup_scene_generate(monkeypatch, shots)
    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("シーングループ生成", 8.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "シーングループ生成",
            "target_duration_sec": 8.0, "backend_name": "mock",
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        # 生成はシーン数（2回）だけ。ショット4本はマスターからの切り出しで揃う
        assert backend.generate_calls == ["sc1", "sc2"]
        # クレジット上限はシーン時「構成ショット数×上限」= 2×30 = 60 で渡っている
        assert backend.seen_credit_limits == [60, 60]
        # 生成後は上限が元(30)へ戻っている
        assert backend.max_credits_per_shot == 30
        # 全ショットに clip_path が確定（従来と同一形式: source_duration=dur, trim={0,dur}）
        clip_shots = saved["plan"]["shots"]
        assert len(clip_shots) == 4
        assert all(s["clip_path"] for s in clip_shots)
        assert clip_shots[0]["trim"] == {"start": 0.0, "end": 2.0}
        assert clip_shots[0]["source_duration"] == 2.0
        # project["scenes"] に2シーンが永続化される
        scenes_meta = saved["scenes"]
        assert [sc["shot_ids"] for sc in scenes_meta] == [["s1", "s2"], ["s3", "s4"]]
        assert scenes_meta[0]["planned_duration_sec"] == 4.0
        assert scenes_meta[0]["actual_duration_sec"] == 4.0
        assert scenes_meta[0]["clip_path"].endswith("scene__sc1.mp4")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_without_scene_id_uses_legacy_per_shot_path(monkeypatch):
    """scene_id 無し: 各ショットが単独シーン=従来経路。generateはショットidで各1回・scenes無し。"""
    shots = [
        {"id": "s1", "visual_prompt": "v1", "motion_preset": "zoom_in", "duration_sec": 3.0, "caption_jp": "c1"},
        {"id": "s2", "visual_prompt": "v2", "motion_preset": "pan_left", "duration_sec": 3.0, "caption_jp": "c2"},
        {"id": "s3", "visual_prompt": "v3", "motion_preset": "static", "duration_sec": 3.0, "caption_jp": "c3"},
    ]
    backend = _setup_scene_generate(monkeypatch, shots)
    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("従来経路", 9.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "従来経路",
            "target_duration_sec": 9.0, "backend_name": "mock",
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        # 各ショットを直接生成（ショットidで呼ばれる）。scene master は作らない
        assert backend.generate_calls == ["s1", "s2", "s3"]
        # 単独シーンなので構成ショット数=1 → 上限スケールは効かず元のまま
        assert backend.seen_credit_limits == [30, 30, 30]
        # 多メンバーシーンが無いので scenes は空
        assert not saved.get("scenes")
        assert all(s["clip_path"] for s in saved["plan"]["shots"])
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_preserves_earlier_successful_clip_path_when_later_shot_fails_bug10(monkeypatch):
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(3))
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _FailsAfterFirstShotBackend())
    # ffmpegは実行せず常に成功したことにする（正規化コマンド自体の妥当性はこのテストの対象外）。
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("BUG-10テスト:部分成功", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"],
            "theme": "BUG-10テスト:部分成功",
            "target_duration_sec": 15.0,
            "backend_name": "mock",
        })

        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"

        shots = saved["plan"]["shots"]
        assert len(shots) == 3
        assert shots[0]["clip_path"] is not None  # 1本目は成功したのでclip_pathが確定している
        assert shots[1]["clip_path"] is None  # 2本目は失敗したので未確定のまま
        assert shots[2]["clip_path"] is None  # 3本目は着手前
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)
