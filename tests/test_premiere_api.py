# -*- coding: utf-8 -*-
"""「Premiereで編集」機能（Phase A配線）のテスト。

対象:
  - premiere.package.build_package（書き出しパッケージ生成本体）
  - studio.server.jobs の "premiere_export" ジョブ種別
  - POST /api/projects/{id}/premiere-export（FastAPIエンドポイント）

TTSは実際の `say`/ネットワークを避けるため、premiere.package.tts_mod.get_tts_backend を
偽実装に差し替える（fish_audio/say/silentの実行を経由しない・高速・決定論的）。
PROJECTS_ROOTはtmp_pathへ差し替え、実プロジェクトディレクトリ(projects/)を汚さない
（tests/test_jobs_generate.pyと同じ隔離パターン）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import xml.dom.minidom as minidom
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import premiere.package as package_mod
from studio.server import jobs as jobs_mod
from studio.server import projects
from studio.server.app import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# 隔離 + 高速TTS偽実装
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


def _make_ready_project(theme="Premiere書き出しテスト", narration_text="ナレーションのテスト文", with_bgm=False):
    project = projects.create_project(theme, 6.0, "mock", status="ready")
    plan = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "", "caption": "テストキャプション",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0},
        }],
        "narration_text": narration_text,
        "bgm": {"file": "upbeat_01.mp3", "gain_db": -14.0, "ducking": True} if with_bgm else None,
        "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    project["plan"] = plan
    projects.save_project(project)
    return project


def _make_job_manager_without_worker(monkeypatch):
    """バックグラウンドワーカースレッドを起動せず、_run_premiere_exportを同期的に呼べるJobManagerを作る。"""
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


# ---------------------------------------------------------------------------
# premiere.package.build_package: 成果物一式
# ---------------------------------------------------------------------------

def test_build_package_creates_all_expected_files():
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])

    package_dir = Path(result["package_dir"])
    assert (package_dir / "reel.xml").exists()
    assert (package_dir / "captions.srt").exists()
    assert (package_dir / "narration.wav").exists()
    assert (package_dir / "style" / "STYLE_SPEC.md").exists()
    assert (package_dir / "README_import.md").exists()
    assert len(result["files"]) == 5
    for f in result["files"]:
        assert Path(f).exists()


def test_build_package_return_value_has_expected_keys():
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])

    assert set(result.keys()) == {"package_dir", "files", "tts", "profile_name"}
    assert result["profile_name"] == "ttp_reference"
    assert result["tts"]["backend"] == "fake"
    assert result["tts"]["duration_sec"] == 1.23


def test_build_package_reel_xml_is_well_formed_and_includes_narration_clip():
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])
    xml_text = (Path(result["package_dir"]) / "reel.xml").read_text(encoding="utf-8")
    minidom.parseString(xml_text)  # 例外が出なければ整形式
    assert "<name>narration</name>" in xml_text
    assert "<name>s1</name>" in xml_text


def test_build_package_captions_srt_matches_srt_module_output():
    from premiere import srt as srt_mod
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])
    captions_text = (Path(result["package_dir"]) / "captions.srt").read_text(encoding="utf-8")
    assert captions_text == srt_mod.build_srt(project["plan"])
    assert "テストキャプション" in captions_text


def test_build_package_style_spec_copied_verbatim():
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])
    copied = (Path(result["package_dir"]) / "style" / "STYLE_SPEC.md").read_text(encoding="utf-8")
    original = package_mod.STYLE_SPEC_SRC.read_text(encoding="utf-8")
    assert copied == original
    assert "Noto Sans JP Black" in copied


def test_build_package_bgm_gain_reflected_in_reel_xml_when_plan_has_bgm():
    project = _make_ready_project(with_bgm=True)
    result = package_mod.build_package(project["id"])
    xml_text = (Path(result["package_dir"]) / "reel.xml").read_text(encoding="utf-8")
    expected_level = 10.0 ** (-14.0 / 20.0)
    assert "<value>{:.6f}</value>".format(expected_level) in xml_text


def test_build_package_missing_project_raises():
    with pytest.raises(package_mod.PremierePackageError):
        package_mod.build_package("p_does_not_exist_xyz")


def test_build_package_produces_distinct_package_dir_on_rapid_repeated_calls():
    """同一プロジェクトへ短時間(同一秒)に複数回build_package()を呼んでも、
    package_dirが衝突しない（BUG修正: premiere_export二重実行レース(a)）。"""
    project = _make_ready_project()
    result1 = package_mod.build_package(project["id"])
    result2 = package_mod.build_package(project["id"])
    assert result1["package_dir"] != result2["package_dir"]
    assert Path(result1["package_dir"]).exists()
    assert Path(result2["package_dir"]).exists()
    # 片方の書き出し内容が他方の展開先を上書きしていないことも確認する
    assert (Path(result1["package_dir"]) / "reel.xml").exists()
    assert (Path(result2["package_dir"]) / "reel.xml").exists()


def test_build_package_progress_cb_receives_increasing_progress_values():
    project = _make_ready_project()
    seen = []
    package_mod.build_package(project["id"], progress_cb=lambda pct, msg: seen.append((pct, msg)))
    assert len(seen) >= 4
    pcts = [p for p, _ in seen]
    assert pcts == sorted(pcts)
    assert all(0 <= p <= 100 for p in pcts)
    assert all(isinstance(msg, str) and msg for _, msg in seen)


# ---------------------------------------------------------------------------
# README_import.md の内容
# ---------------------------------------------------------------------------

def test_build_package_readme_contains_import_steps_and_troubleshooting():
    project = _make_ready_project()
    result = package_mod.build_package(project["id"])
    readme = (Path(result["package_dir"]) / "README_import.md").read_text(encoding="utf-8")

    assert "reel.xml" in readme
    assert "captions.srt" in readme
    assert "エッセンシャルグラフィックス" in readme
    assert "トラックスタイル" in readme
    assert "メディアがオフライン" in readme
    assert "projects/{}/clips/".format(project["id"]) in readme


# ---------------------------------------------------------------------------
# studio.server.jobs: premiere_export ジョブ種別
# ---------------------------------------------------------------------------

def test_try_start_premiere_export_returns_job_id_for_ready_project(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    job_id = manager.try_start_premiere_export(project["id"])
    assert job_id is not None
    snap = manager.get_snapshot(job_id)
    assert snap["kind"] == "premiere_export"


def test_try_start_premiere_export_raises_for_non_ready_project(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("未完成テスト", 6.0, "mock", status="draft")
    with pytest.raises(jobs_mod.PremiereExportNotAllowedError):
        manager.try_start_premiere_export(project["id"])


def test_try_start_premiere_export_returns_none_for_unknown_project(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    assert manager.try_start_premiere_export("p_does_not_exist_xyz") is None


def test_try_start_premiere_export_second_call_while_in_progress_is_rejected(monkeypatch):
    """同一project_idへの二重投入は2回目が拒否される（BUG修正: premiere_export二重実行レース(b)）。"""
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()

    job_id_1 = manager.try_start_premiere_export(project["id"])
    assert job_id_1 is not None

    with pytest.raises(jobs_mod.PremiereExportInProgressError):
        manager.try_start_premiere_export(project["id"])
    # PremiereExportInProgressErrorはPremiereExportNotAllowedErrorのサブクラス
    # （app.pyの `except PremiereExportNotAllowedError:` が409として捕捉できることを確認）。
    assert issubclass(jobs_mod.PremiereExportInProgressError, jobs_mod.PremiereExportNotAllowedError)


def test_try_start_premiere_export_allowed_again_after_job_completes(monkeypatch):
    """ジョブが完了(成功)すれば、同一project_idへの再投入が再び受理される。"""
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()

    job_id_1 = manager.try_start_premiere_export(project["id"])
    manager._run_premiere_export(job_id_1, {"project_id": project["id"]})

    job_id_2 = manager.try_start_premiere_export(project["id"])
    assert job_id_2 is not None
    assert job_id_2 != job_id_1


def test_try_start_premiere_export_allowed_again_after_job_fails(monkeypatch):
    """ジョブが失敗して終了した場合も、in_progressロックが解放され再投入が受理される。"""
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()

    def _boom(project_id, progress_cb=None):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(jobs_mod.premiere_package, "build_package", _boom)

    job_id_1 = manager.try_start_premiere_export(project["id"])
    manager._run_premiere_export(job_id_1, {"project_id": project["id"]})

    job_id_2 = manager.try_start_premiere_export(project["id"])
    assert job_id_2 is not None


def test_run_premiere_export_does_not_change_project_status_or_renders(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()
    manager._run_premiere_export("job_test_1", {"project_id": project["id"]})

    reloaded = projects.get_project(project["id"])
    assert reloaded["status"] == "ready"
    assert reloaded["renders"] == []

    snap = manager.get_snapshot("job_test_1")
    assert snap["done"] is True
    assert snap["ok"] is True
    assert snap["path"] is not None
    assert Path(snap["path"]).exists()


def test_run_premiere_export_failure_records_error_history_and_keeps_status_ready(monkeypatch):
    manager = _make_job_manager_without_worker(monkeypatch)
    project = _make_ready_project()

    def _boom(project_id, progress_cb=None):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(jobs_mod.premiere_package, "build_package", _boom)
    manager._run_premiere_export("job_test_fail", {"project_id": project["id"]})

    reloaded = projects.get_project(project["id"])
    assert reloaded["status"] == "ready"  # statusは変更しない
    assert reloaded["renders"] == []
    history = reloaded["error_history"]
    assert len(history) == 1
    assert history[0]["kind"] == "premiere_export"
    assert "simulated failure" in history[0]["message"]

    snap = manager.get_snapshot("job_test_fail")
    assert snap["done"] is True
    assert snap["ok"] is False


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/premiere-export
# ---------------------------------------------------------------------------

def test_premiere_export_endpoint_returns_404_for_unknown_project():
    resp = client.post("/api/projects/p_does_not_exist_xyz/premiere-export")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("status", ["draft", "generating", "rendering", "failed"])
def test_premiere_export_endpoint_returns_409_for_non_ready_status(status):
    project = projects.create_project("非readyテスト:{}".format(status), 6.0, "mock", status=status)
    resp = client.post("/api/projects/{}/premiere-export".format(project["id"]))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_premiere_export_endpoint_returns_202_with_job_id_for_ready_project():
    project = _make_ready_project()
    resp = client.post("/api/projects/{}/premiere-export".format(project["id"]))
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body and body["job_id"]


def _drain_sse_until_done(path, timeout_sec=30):
    """SSEストリームを読み、`{"done":true,...}` イベントが来るまでブロックして返す。"""
    import json
    import time
    deadline = time.time() + timeout_sec
    with client.stream("GET", path) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if time.time() > deadline:
                raise TimeoutError("SSEが時間内に完了しませんでした: {}".format(path))
            if not line or not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: "):])
            if payload.get("done"):
                return payload
    raise AssertionError("SSEストリームが完了イベントなしに終了しました: {}".format(path))


def test_premiere_export_sse_completes_with_ok_true_and_package_dir_path():
    project = _make_ready_project()
    resp = client.post("/api/projects/{}/premiere-export".format(project["id"]))
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    done_event = _drain_sse_until_done("/api/jobs/{}/events".format(job_id))
    assert done_event["ok"] is True
    assert done_event["path"]
    package_dir = Path(done_event["path"])
    assert package_dir.exists()
    assert (package_dir / "reel.xml").exists()
    assert (package_dir / "README_import.md").exists()

    # project.status/rendersは不変(readyのまま)であることをAPI越しにも確認する
    reloaded = client.get("/api/projects/{}".format(project["id"])).json()
    assert reloaded["status"] == "ready"
    assert reloaded["renders"] == []
