# -*- coding: utf-8 -*-
"""Premiere Pro自動化(Phase B)のテスト。

対象:
  - premiere.setup_check.check_setup（pymiere import可否/Pymiere Link疎通/Premiere起動確認）
  - premiere.driver.run_import（pymiere経由の自動インポート本体。pymiereはsys.modules注入でモックする）
  - studio.server.jobs._run_premiere_export のPhase B自動実行分岐
    （ready時にdriver.run_importが呼ばれる/未ready時に呼ばれない/premiere_exports履歴の追記/
     Phase Bが失敗してもジョブは常にok:trueで終わる）

pymiereは実際にはインストールされていない前提のテスト環境のため、run_importのテストは
`import pymiere` が成功するよう sys.modules["pymiere"] へ偽モジュールを注入してから呼び出す
（premiere.driver.run_importは関数内でimportするため、この注入方式が機能する）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

import premiere.package as package_mod
from premiere import driver as driver_mod
from premiere import setup_check
from studio.server import jobs as jobs_mod
from studio.server import projects


# ---------------------------------------------------------------------------
# premiere.setup_check.check_setup
# ---------------------------------------------------------------------------

def test_check_setup_all_ready_returns_ready_true_and_empty_missing():
    result = setup_check.check_setup(
        check_pymiere_importable=lambda: True,
        check_pymiere_link_reachable=lambda: True,
        check_premiere_running=lambda: True,
    )
    assert result == {"ready": True, "missing": []}


def test_check_setup_pymiere_not_importable_adds_missing_reason():
    result = setup_check.check_setup(
        check_pymiere_importable=lambda: False,
        check_pymiere_link_reachable=lambda: True,
        check_premiere_running=lambda: True,
    )
    assert result["ready"] is False
    assert result["missing"] == ["pymiere_not_installed"]


def test_check_setup_link_unreachable_adds_missing_reason():
    result = setup_check.check_setup(
        check_pymiere_importable=lambda: True,
        check_pymiere_link_reachable=lambda: False,
        check_premiere_running=lambda: True,
    )
    assert result["ready"] is False
    assert result["missing"] == ["pymiere_link_unreachable"]


def test_check_setup_premiere_not_running_adds_missing_reason():
    result = setup_check.check_setup(
        check_pymiere_importable=lambda: True,
        check_pymiere_link_reachable=lambda: True,
        check_premiere_running=lambda: False,
    )
    assert result["ready"] is False
    assert result["missing"] == ["premiere_not_running"]


def test_check_setup_multiple_missing_combines_all_reasons_in_check_order():
    result = setup_check.check_setup(
        check_pymiere_importable=lambda: False,
        check_pymiere_link_reachable=lambda: False,
        check_premiere_running=lambda: False,
    )
    assert result["ready"] is False
    assert result["missing"] == [
        "pymiere_not_installed", "pymiere_link_unreachable", "premiere_not_running",
    ]


def test_check_setup_injected_check_raising_exception_is_treated_as_missing():
    def _boom():
        raise RuntimeError("injected failure")

    result = setup_check.check_setup(
        check_pymiere_importable=_boom,
        check_pymiere_link_reachable=lambda: True,
        check_premiere_running=lambda: True,
    )
    assert result["ready"] is False
    assert "pymiere_not_installed" in result["missing"]


# ---------------------------------------------------------------------------
# premiere.driver.run_import: 共通のフェイクpymiereモジュール
# ---------------------------------------------------------------------------

class _FakeSequence:
    def __init__(self):
        self.videoTracks = Mock()
        self.createCaptionTrack = Mock()


class _FakeProjectItem:
    def __init__(self, name):
        self.name = name


class _FakeChildren:
    def __init__(self, items):
        self._items = list(items)

    @property
    def numItems(self):
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def append(self, item):
        self._items.append(item)


class _FakeRootItem:
    def __init__(self, items=None):
        self.children = _FakeChildren(items or [])


class _FakeProject:
    def __init__(self, active_sequence=None, root_items=None):
        self.activeSequence = active_sequence
        self.rootItem = _FakeRootItem(root_items)
        self.importFiles = Mock()
        self.save = Mock()


class _FakeApp:
    def __init__(self, project):
        self.project = project
        self.newProject = Mock()


def _install_fake_pymiere(monkeypatch, app):
    fake_pymiere = types.ModuleType("pymiere")
    fake_pymiere.objects = types.SimpleNamespace(app=app)
    monkeypatch.setitem(sys.modules, "pymiere", fake_pymiere)


def _package_dir_with_reel_xml(tmp_path, with_captions=True):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "reel.xml").write_text("<xmeml/>", encoding="utf-8")
    if with_captions:
        (package_dir / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    return package_dir


# ---------------------------------------------------------------------------
# premiere.driver.run_import: 正常系・縮退・例外系
# ---------------------------------------------------------------------------

def test_run_import_success_creates_project_imports_reel_and_captions_and_saves(tmp_path, monkeypatch):
    sequence = _FakeSequence()
    project = _FakeProject(active_sequence=sequence, root_items=[_FakeProjectItem("captions.srt")])
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is True
    assert result["prproj_path"] == str(package_dir / "reel.prproj")
    assert result["caption_track_created"] is True
    app.newProject.assert_called_once_with(str(package_dir / "reel.prproj"))
    assert project.importFiles.call_count == 2
    project.importFiles.assert_any_call([str(package_dir / "reel.xml")])
    project.importFiles.assert_any_call([str(package_dir / "captions.srt")])
    sequence.createCaptionTrack.assert_called_once()
    project.save.assert_called_once()


def test_run_import_calls_progress_cb_with_increasing_progress_in_range_90_to_98(tmp_path, monkeypatch):
    sequence = _FakeSequence()
    project = _FakeProject(active_sequence=sequence, root_items=[_FakeProjectItem("captions.srt")])
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path)

    seen = []
    driver_mod.run_import(str(package_dir), "p_test1", progress_cb=lambda pct, msg: seen.append((pct, msg)))

    assert len(seen) >= 3
    pcts = [p for p, _ in seen]
    assert pcts == sorted(pcts)
    assert all(90 <= p <= 98 for p in pcts)


def test_run_import_caption_track_creation_failure_degrades_but_stays_ok(tmp_path, monkeypatch):
    sequence = _FakeSequence()
    sequence.createCaptionTrack.side_effect = RuntimeError("ExtendScript API mismatch")
    project = _FakeProject(active_sequence=sequence, root_items=[_FakeProjectItem("captions.srt")])
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is True
    assert result["caption_track_created"] is False
    assert "タイムラインへドラッグ" in result["detail"]
    project.save.assert_called_once()  # 縮退後も保存は行う


def test_run_import_missing_captions_srt_skips_caption_step_but_succeeds(tmp_path, monkeypatch):
    sequence = _FakeSequence()
    project = _FakeProject(active_sequence=sequence, root_items=[])
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path, with_captions=False)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is True
    assert result["caption_track_created"] is False
    assert project.importFiles.call_count == 1  # reel.xmlのみ


def test_run_import_reel_xml_missing_returns_ok_false_with_detail(tmp_path, monkeypatch):
    app = _FakeApp(_FakeProject())
    _install_fake_pymiere(monkeypatch, app)
    empty_package_dir = tmp_path / "empty_package"
    empty_package_dir.mkdir()

    result = driver_mod.run_import(str(empty_package_dir), "p_test1")

    assert result["ok"] is False
    assert result["prproj_path"] is None
    assert "reel.xml" in result["detail"]
    app.newProject.assert_not_called()


def test_run_import_pymiere_not_installed_returns_ok_false_japanese_detail(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymiere", None)  # importが必ずImportErrorになる
    package_dir = _package_dir_with_reel_xml(tmp_path)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is False
    assert result["prproj_path"] is None
    assert result["caption_track_created"] is False
    assert "インストールされていない" in result["detail"]


def test_run_import_sequence_not_found_returns_ok_false_with_detail(tmp_path, monkeypatch):
    project = _FakeProject(active_sequence=None, root_items=[_FakeProjectItem("captions.srt")])
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is False
    assert "シーケンスが見つかりません" in result["detail"]


def test_run_import_unexpected_exception_from_pymiere_call_is_caught_with_japanese_detail(tmp_path, monkeypatch):
    project = _FakeProject()
    project.importFiles.side_effect = RuntimeError("boom-unexpected")
    app = _FakeApp(project)
    _install_fake_pymiere(monkeypatch, app)
    package_dir = _package_dir_with_reel_xml(tmp_path)

    result = driver_mod.run_import(str(package_dir), "p_test1")

    assert result["ok"] is False
    assert result["prproj_path"] is None
    assert "boom-unexpected" in result["detail"]


# ---------------------------------------------------------------------------
# studio.server.jobs: _run_premiere_export のPhase B自動実行分岐
# ---------------------------------------------------------------------------

class _FakeTTSBackend:
    name = "fake"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")
        return {
            "backend": "fake", "duration_sec": 1.23, "is_silent": False,
            "requested_backend": "fake", "fallback_reason": None,
        }


def _fake_get_tts_backend(voice="Kyoko", cfg=None):
    return _FakeTTSBackend()


@pytest.fixture(autouse=True)
def _isolated_projects_root_and_fake_tts(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(package_mod.tts_mod, "get_tts_backend", _fake_get_tts_backend)
    yield


def _make_ready_project(theme="Premiere自動化テスト"):
    project = projects.create_project(theme, 6.0, "mock", status="ready")
    plan = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "", "caption": "テストキャプション",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0},
        }],
        "narration_text": "ナレーションのテスト文",
        "bgm": None,
        "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    project["plan"] = plan
    projects.save_project(project)
    return project


def _make_job_manager_without_worker(monkeypatch):
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def test_jobs_ready_setup_calls_driver_run_import_with_package_dir_and_project_id(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    monkeypatch.setattr(jobs_mod.premiere_setup_check, "check_setup", lambda: {"ready": True, "missing": []})
    spy = Mock(return_value={
        "ok": True, "prproj_path": "/tmp/fake.prproj", "caption_track_created": True, "detail": "ok",
    })
    monkeypatch.setattr(jobs_mod.premiere_driver, "run_import", spy)

    manager._run_premiere_export("job_ready", {"project_id": project["id"]})

    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args[0] and Path(args[0]).exists()  # package_dir
    assert args[1] == project["id"]


def test_jobs_not_ready_setup_does_not_call_driver_run_import(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    monkeypatch.setattr(
        jobs_mod.premiere_setup_check, "check_setup",
        lambda: {"ready": False, "missing": ["pymiere_not_installed"]},
    )
    spy = Mock()
    monkeypatch.setattr(jobs_mod.premiere_driver, "run_import", spy)

    manager._run_premiere_export("job_not_ready", {"project_id": project["id"]})

    spy.assert_not_called()
    snap = manager.get_snapshot("job_not_ready")
    assert snap["ok"] is True  # パッケージ自体は有効なのでジョブは成功扱い


def test_jobs_appends_premiere_exports_history_with_expected_keys(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    monkeypatch.setattr(jobs_mod.premiere_setup_check, "check_setup", lambda: {"ready": True, "missing": []})
    monkeypatch.setattr(
        jobs_mod.premiere_driver, "run_import",
        lambda package_dir, project_id, progress_cb=None: {
            "ok": True, "prproj_path": package_dir + "/reel.prproj",
            "caption_track_created": True, "detail": "ok",
        },
    )

    manager._run_premiere_export("job_history", {"project_id": project["id"]})

    reloaded = projects.get_project(project["id"])
    exports = reloaded["premiere_exports"]
    assert len(exports) == 1
    entry = exports[0]
    assert set(entry.keys()) == {"ts", "package_dir", "prproj_path", "auto"}
    assert entry["auto"] is True
    assert entry["prproj_path"].endswith("reel.prproj")
    assert reloaded["status"] == "ready"  # statusは不変
    assert reloaded["renders"] == []  # rendersは不変


def test_jobs_driver_failure_still_finishes_job_ok_true_with_auto_false(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    monkeypatch.setattr(jobs_mod.premiere_setup_check, "check_setup", lambda: {"ready": True, "missing": []})

    def _boom(package_dir, project_id, progress_cb=None):
        raise RuntimeError("simulated driver crash")

    monkeypatch.setattr(jobs_mod.premiere_driver, "run_import", _boom)

    manager._run_premiere_export("job_driver_fail", {"project_id": project["id"]})

    snap = manager.get_snapshot("job_driver_fail")
    assert snap["done"] is True
    assert snap["ok"] is True  # Phase Bが落ちてもPhase Aのパッケージは有効なので成功扱い
    assert snap.get("auto") is False

    reloaded = projects.get_project(project["id"])
    exports = reloaded["premiere_exports"]
    assert exports[-1]["auto"] is False
    assert exports[-1]["prproj_path"] is None
