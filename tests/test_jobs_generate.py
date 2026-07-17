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
        return plan_schema.build_rule_based_plan(
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
