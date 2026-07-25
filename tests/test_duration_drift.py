# -*- coding: utf-8 -*-
"""尺乖離の可視化（director企画時点のショット合計尺 vs 同期モード後の実出力尺）の回帰テスト。

独立レビュー指摘: 音声主導タイミング同期モードでは各ショットの表示尺が断片TTSの実測尺に
合わせて伸縮するため、director企画時点の目標尺と実際の出力尺がずれることがあるが、
そのずれがどこにも記録・通知されていなかった。

修正:
  - run.py: report["stages"]["render"]["duration_drift_sec"] を常に記録し、
    |drift|>3.0秒ならreport["stages"]["render"]["duration_drift_warning"]も記録する。
  - studio/server/jobs.py: project["tts"]["duration_drift_sec"] を常に記録し、
    |drift|>3.0秒ならSSEで警告メッセージを1回emitする。
  - いずれもQAの合否判定式（qa/qa_check.py）は変更しない（可視化のみ）。
"""
from queue import Empty

import pytest

import run
from pipeline.config import load_config
from studio.server import jobs as jobs_mod
from studio.server import projects


# ---------------------------------------------------------------------------
# jobs_mod._duration_drift_info（純粋関数）の単体テスト
# ---------------------------------------------------------------------------

def test_duration_drift_info_returns_none_when_target_missing():
    drift, warning = jobs_mod._duration_drift_info(10.0, None)
    assert drift is None
    assert warning is None


def test_duration_drift_info_no_warning_within_threshold():
    drift, warning = jobs_mod._duration_drift_info(10.0, 8.0)
    assert drift == pytest.approx(2.0)
    assert warning is None


def test_duration_drift_info_warns_when_output_longer_than_target():
    drift, warning = jobs_mod._duration_drift_info(20.0, 10.0)
    assert drift == pytest.approx(10.0)
    assert warning is not None
    assert "10.0秒長く" in warning


def test_duration_drift_info_warns_when_output_shorter_than_target():
    drift, warning = jobs_mod._duration_drift_info(5.0, 10.0)
    assert drift == pytest.approx(-5.0)
    assert warning is not None
    assert "5.0秒短く" in warning


# ---------------------------------------------------------------------------
# run.py: run_pipeline経由（同期モード。CLIパス）
# ---------------------------------------------------------------------------

_DRIFT_TEST_SHOTS = [
    {
        "id": "s1", "visual_prompt": "abstract a", "motion_preset": "static", "duration_sec": 2.0,
        "caption_jp": "テロップ1", "narration_jp": "断片1です。",
    },
    {
        "id": "s2", "visual_prompt": "abstract b", "motion_preset": "static", "duration_sec": 5.0,
        "caption_jp": "テロップ2", "narration_jp": "断片2です。",
    },
]


def _fake_plan_with_shots(shots, narration_script="断片1です。断片2です。"):
    return {
        "version": 1, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
        "narration_script": narration_script, "shots": shots, "bgm_mood": "none",
    }


@pytest.mark.slow
def test_run_pipeline_records_small_drift_without_warning(monkeypatch):
    """director企画時点の合計尺(2.0+5.0=7.0秒)に対し、断片TTSの表示尺合計(4.5秒)の差は
    2.5秒(<=3.0)のため、drift_secは記録されるがwarningは出ない。"""
    cfg = load_config()
    monkeypatch.setattr(run.director, "run_director", lambda theme, config, **kw: _fake_plan_with_shots(_DRIFT_TEST_SHOTS))

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 3.0},  # display=3.25
        {"path": "/tmp/seg1.wav", "duration_sec": 1.0},  # display=1.25
    ]
    monkeypatch.setattr(
        run.tts_mod, "synthesize_segments",
        lambda texts, out_dir, cfg_, voice="Kyoko", **_kw: {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None},
    )
    monkeypatch.setattr(run.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    report = run.run_pipeline("尺乖離テスト:小さいずれ", 7.0, "mock", True, cfg, quality="single", style="default")

    render_stage = report["stages"]["render"]
    assert render_stage["duration_drift_sec"] == pytest.approx((3.25 + 1.25) - 7.0)
    assert "duration_drift_warning" not in render_stage


@pytest.mark.slow
def test_run_pipeline_records_large_drift_with_warning(monkeypatch):
    """断片TTSの表示尺合計(2.5秒)がdirector企画時点の合計尺(7.0秒)より4.5秒短い
    (>3.0秒)ため、duration_drift_warningが記録される。"""
    cfg = load_config()
    monkeypatch.setattr(run.director, "run_director", lambda theme, config, **kw: _fake_plan_with_shots(_DRIFT_TEST_SHOTS))

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 1.0},  # display=1.25
        {"path": "/tmp/seg1.wav", "duration_sec": 1.0},  # display=1.25
    ]
    monkeypatch.setattr(
        run.tts_mod, "synthesize_segments",
        lambda texts, out_dir, cfg_, voice="Kyoko", **_kw: {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None},
    )
    monkeypatch.setattr(run.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    report = run.run_pipeline("尺乖離テスト:大きいずれ", 7.0, "mock", True, cfg, quality="single", style="default")

    render_stage = report["stages"]["render"]
    expected_drift = (1.25 + 1.25) - 7.0
    assert render_stage["duration_drift_sec"] == pytest.approx(expected_drift)
    assert "duration_drift_warning" in render_stage
    assert "短く" in render_stage["duration_drift_warning"]
    assert "{:.1f}秒".format(abs(expected_drift)) in render_stage["duration_drift_warning"]

    # QAの合否判定式そのものは変えていないこと（drift自体はrender stageのokに影響しない）。
    assert render_stage["ok"] is True


@pytest.mark.slow
def test_run_pipeline_warns_on_raw_drift_just_above_threshold_even_if_rounded_value_is_exactly_3_0(monkeypatch):
    """独立レビュー指摘の回帰テスト: 閾値判定はround()後の値ではなく生の差分で行うこと。
    生drift=3.004秒(round後は3.0秒)でも|drift|>3.0を満たすのでwarningが出る必要がある
    （round()を先に適用すると3.0 > 3.0がFalseになり誤ってwarningが消える）。"""
    cfg = load_config()
    monkeypatch.setattr(run.director, "run_director", lambda theme, config, **kw: _fake_plan_with_shots(_DRIFT_TEST_SHOTS))

    # display = duration_sec + 0.25。合計displayを 10.004 (=total_shot_duration 7.0 + 3.004) にする。
    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 6.0},     # display=6.25
        {"path": "/tmp/seg1.wav", "duration_sec": 3.504},   # display=3.754
    ]
    monkeypatch.setattr(
        run.tts_mod, "synthesize_segments",
        lambda texts, out_dir, cfg_, voice="Kyoko", **_kw: {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None},
    )
    monkeypatch.setattr(run.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    report = run.run_pipeline("尺乖離テスト:丸め誤差で閾値見逃し回帰", 7.0, "mock", True, cfg, quality="single", style="default")

    render_stage = report["stages"]["render"]
    assert render_stage["duration_drift_sec"] == pytest.approx(3.0, abs=0.01)
    assert "duration_drift_warning" in render_stage
    assert "長く" in render_stage["duration_drift_warning"]


# ---------------------------------------------------------------------------
# studio/server/jobs.py: _run_render経由（全文方式・実ffmpeg/実TTSはmonkeypatchで排除）
# ---------------------------------------------------------------------------

def _make_project_for_render(theme, target_duration_sec, trim_end):
    project = projects.create_project(theme, target_duration_sec, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "s1.mp4"
    clip_path.write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract", "caption": "テスト",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": trim_end, "trim": {"start": 0.0, "end": trim_end},
        }],
        "narration_text": "テストナレーション",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)
    return project


class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        from pathlib import Path
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 5.0, "is_silent": False}


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _job_manager_without_worker(monkeypatch):
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def _drain(q):
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except Empty:
            break
    return events


def test_run_render_records_drift_and_emits_warning_once_when_large(monkeypatch):
    project = _make_project_for_render("尺乖離テスト:jobs大きく超過", target_duration_sec=5.0, trim_end=12.0)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())

    manager = _job_manager_without_worker(monkeypatch)
    job_id = "job_drift_test_large"
    q = manager.subscribe(job_id)

    manager._run_render(job_id, {"project_id": project["id"]})

    saved = projects.get_project(project["id"])
    assert saved["status"] == "ready"
    assert saved["tts"]["duration_drift_sec"] == pytest.approx(12.0 - 5.0)

    warnings = [e for e in _drain(q) if e.get("message") and "目標尺より" in e["message"]]
    assert len(warnings) == 1
    assert "7.0秒長く" in warnings[0]["message"]


def test_run_render_records_drift_without_warning_when_small(monkeypatch):
    project = _make_project_for_render("尺乖離テスト:jobs誤差範囲", target_duration_sec=5.0, trim_end=6.0)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())

    manager = _job_manager_without_worker(monkeypatch)
    job_id = "job_drift_test_small"
    q = manager.subscribe(job_id)

    manager._run_render(job_id, {"project_id": project["id"]})

    saved = projects.get_project(project["id"])
    assert saved["tts"]["duration_drift_sec"] == pytest.approx(1.0)

    warnings = [e for e in _drain(q) if e.get("message") and "目標尺より" in e["message"]]
    assert warnings == []
