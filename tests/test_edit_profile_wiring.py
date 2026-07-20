# -*- coding: utf-8 -*-
"""studio/server/jobs.py `_render_project` への edit enhancement層(edit_profile)配線の検証。

実ffmpegは実行しない（jobs_mod.render.run_ffmpegをmonkeypatchしコマンド列を検証する）。
project["edit_profile_applied"]の記録と、edit_profile層の例外時フォールバック
（従来コマンドと等価になること）を確認する。
"""
from pathlib import Path

import pytest

from studio.server import jobs as jobs_mod
from studio.server import projects


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_project_with_two_shots(theme):
    project = projects.create_project(theme, 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for idx, (start, end) in enumerate([(0.0, 2.0), (0.0, 3.0)], start=1):
        sid = "s{}".format(idx)
        clip_path = clips_dir / "{}.mp4".format(sid)
        clip_path.write_bytes(b"\x00")
        shots.append({
            "id": sid, "order": idx - 1, "enabled": True, "prompt": "abstract", "caption": "テスト{}".format(idx),
            "clip_path": projects.media_relpath_for_clip(project["id"], "{}.mp4".format(sid)),
            "source_duration": end, "trim": {"start": start, "end": end},
        })
    project["plan"] = {
        "shots": shots, "narration_text": "全文テストナレーション。",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)
    return project


class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 5.0, "is_silent": False}


def _capture_ffmpeg(monkeypatch):
    calls = []

    def fake_run_ffmpeg(cmd, timeout_sec=None):
        calls.append(cmd)
        return {"returncode": 0, "stderr": ""}

    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", fake_run_ffmpeg)
    return calls


def _setup_full_mode(monkeypatch):
    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full modeではsynthesize_segmentsは呼ばれてはいけない")),
    )
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None: _FakeFullBackend())


def test_render_project_records_edit_profile_applied_true_on_success(monkeypatch):
    project = _make_project_with_two_shots("edit配線テスト成功")
    _setup_full_mode(monkeypatch)
    _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    jobs_mod._render_project(project["id"], project["plan"], cfg)

    saved = projects.get_project(project["id"])
    assert saved["edit_profile_applied"] is True


def test_render_project_final_cmd_includes_cut_sfx_from_edit_profile(monkeypatch):
    """mockバックエンド既定のプロジェクトでも、カットSEはskip_if_backend対象外なので付与される。

    2026-07: カットSEはローテーション化されたため、特定ファイル名(whoosh_01.wav)ではなく
    「assets/sfx/*.wav が -i 入力に混じっているか」で確認する。
    """
    project = _make_project_with_two_shots("カットSE配線テスト")
    _setup_full_mode(monkeypatch)
    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    jobs_mod._render_project(project["id"], project["plan"], cfg)

    final_cmds = [c for c in calls if "-filter_complex" in c]
    assert final_cmds, "final_cmd(filter_complex使用)が見つからない"
    # ローテーションで選ばれる manifest 上のいずれかの wav がSFX入力に含まれる。
    def _has_sfx_input(cmd):
        joined = " ".join(cmd)
        return "assets/sfx/" in joined and ".wav" in joined
    assert any(_has_sfx_input(c) for c in final_cmds)


def test_render_project_falls_back_when_edit_profile_load_raises(monkeypatch):
    """edit_profile.load_edit_profile自体が例外を出しても、レンダは従来どおり完走しapplied=Falseになる。"""
    project = _make_project_with_two_shots("edit配線フォールバックテスト")
    _setup_full_mode(monkeypatch)
    calls = _capture_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        jobs_mod.edit_profile, "load_edit_profile",
        lambda cfg=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert out_rel is not None
    saved = projects.get_project(project["id"])
    assert saved["edit_profile_applied"] is False
    final_cmds = [c for c in calls if "-filter_complex" in c]
    assert not any("whoosh_01.wav" in " ".join(c) for c in final_cmds)


def test_render_project_falls_back_when_enhancement_computation_raises(monkeypatch):
    """edit_profileは読めるが、compute_edit_enhancement_kwargs内で例外が起きても従来コマンドへ
    フォールバックする（edit_profile_applied=False・レンダ自体は落ちない）。"""
    project = _make_project_with_two_shots("edit配線計算フォールバックテスト")
    _setup_full_mode(monkeypatch)
    calls = _capture_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        jobs_mod.render, "compute_edit_enhancement_kwargs",
        lambda durations, profile: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert out_rel is not None
    saved = projects.get_project(project["id"])
    assert saved["edit_profile_applied"] is False
    final_cmds = [c for c in calls if "-filter_complex" in c]
    assert not any("whoosh_01.wav" in " ".join(c) for c in final_cmds)


def test_render_project_skips_punch_in_for_mock_backend(monkeypatch):
    """project["backend"]="mock"(既定)は edit.punch_in.skip_if_backend="mock" によりズーム二重掛けを避ける。"""
    project = _make_project_with_two_shots("mockパンチインskipテスト")
    assert project["backend"] == "mock"
    _setup_full_mode(monkeypatch)
    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    jobs_mod._render_project(project["id"], project["plan"], cfg)

    normalize_cmds = [c for c in calls if "-vf" in c]
    assert normalize_cmds
    assert not any("zoompan" in " ".join(c) for c in normalize_cmds)
