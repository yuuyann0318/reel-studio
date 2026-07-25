# -*- coding: utf-8 -*-
"""studio/server/jobs.py `_render_project` の音声主導タイミング同期モード（segment）と
全文方式（full）フォールバックの単体テスト。

実ffmpeg/実TTSは一切実行しない（jobs_mod.render.run_ffmpeg と jobs_mod.tts_mod の
synthesize_segments/get_tts_backend をすべてmonkeypatchし、構築されたコマンド列を検証する）。
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


def _make_project_with_two_shots(theme, trims):
    """trims: [(start,end), (start,end)]（s1,s2のtrim範囲）。"""
    project = projects.create_project(theme, 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for idx, (start, end) in enumerate(trims, start=1):
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


def _capture_ffmpeg(monkeypatch):
    calls = []

    def fake_run_ffmpeg(cmd, timeout_sec=None):
        calls.append(cmd)
        return {"returncode": 0, "stderr": ""}

    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", fake_run_ffmpeg)
    return calls


class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 5.0, "is_silent": False}


def test_render_project_sync_mode_pads_short_trim_and_truncates_long_trim(monkeypatch):
    """断片実測尺+0.25秒がtrim尺より長いショットはtpadで埋め、短いショットは単純truncateする。"""
    project = _make_project_with_two_shots("同期モードテスト", [(0.0, 2.0), (0.0, 5.0)])
    project["narration_segments"] = {"s1": "断片1のテキストです。", "s2": "断片2のテキストです。"}
    projects.save_project(project)

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 3.0},  # display = 3.25 > trim(2.0) -> pad
        {"path": "/tmp/seg1.wav", "duration_sec": 1.0},  # display = 1.25 < trim(5.0) -> truncate
    ]

    def fake_synthesize_segments(texts, out_dir, cfg, voice="Kyoko", **_kw):
        assert texts == ["断片1のテキストです。", "断片2のテキストです。"]
        return {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None}

    monkeypatch.setattr(jobs_mod.tts_mod, "synthesize_segments", fake_synthesize_segments)
    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "segment"
    assert tts_meta["backend"] == "fake_seg"
    assert out_duration == pytest.approx(3.25 + 1.25)

    norm_cmd_s1, norm_cmd_s2 = calls[0], calls[1]
    assert "tpad=stop_mode=clone:stop_duration=1.250" in " ".join(norm_cmd_s1)
    assert norm_cmd_s1[norm_cmd_s1.index("-t") + 1] == "3.250"
    assert "tpad" not in " ".join(norm_cmd_s2)
    assert norm_cmd_s2[norm_cmd_s2.index("-t") + 1] == "1.250"

    # calls[2] = concat, calls[3] = ナレーション断片配置concat
    narration_cmd = calls[3]
    joined = " ".join(narration_cmd)
    assert "adelay=0|0" in joined
    assert "adelay=3250|3250" in joined  # s2の開始位置 = s1の表示尺(3.25秒)


def test_render_project_sync_mode_ignores_missing_narration_jp_on_disabled_shots(monkeypatch):
    """narration_jpが欠けているのが無効(enabled=False)ショットだけなら、同期モードの判定は
    有効ショットのみを見るため通常どおり有効になる。"""
    project = _make_project_with_two_shots("無効ショット除外テスト", [(0.0, 2.0), (0.0, 2.0)])
    plan = project["plan"]
    plan["shots"].append({
        "id": "s3", "order": 2, "enabled": False, "prompt": "abstract", "caption": "無効ショット",
        "clip_path": None, "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0},
    })
    projects.save_project(project)
    # narration_segmentsにはs3(無効ショット)分を含めない。
    project["narration_segments"] = {"s1": "断片1", "s2": "断片2"}
    projects.save_project(project)

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 1.0},
        {"path": "/tmp/seg1.wav", "duration_sec": 1.0},
    ]
    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda texts, out_dir, cfg, voice="Kyoko", **_kw: {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None},
    )
    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], plan, cfg)

    assert tts_meta["mode"] == "segment"


def test_render_project_without_narration_segments_falls_back_to_full_mode(monkeypatch):
    """narration_segmentsキー自体が無い旧プロジェクトは、従来どおり全文方式でtrim尺のまま。"""
    project = _make_project_with_two_shots("後方互換テスト", [(0.0, 2.0), (0.0, 3.0)])
    assert "narration_segments" not in project

    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full modeではsynthesize_segmentsは呼ばれてはいけない")),
    )
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())

    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "full"
    assert tts_meta["backend"] == "fake_full"
    assert out_duration == pytest.approx(2.0 + 3.0)  # trim尺の合計（従来どおり）
    assert "tpad" not in " ".join(calls[0])
    assert "tpad" not in " ".join(calls[1])


def test_render_project_partial_narration_segments_falls_back_to_full_mode(monkeypatch):
    """有効ショットの一部にしかnarration_jpが無い場合は同期モードを試みず全文方式にする。"""
    project = _make_project_with_two_shots("部分欠けテスト", [(0.0, 2.0), (0.0, 3.0)])
    project["narration_segments"] = {"s1": "断片1だけあります。"}  # s2が欠けている
    projects.save_project(project)

    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("欠けがあるのに呼ばれてはいけない")),
    )
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())

    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "full"


def test_render_project_segment_tts_failure_falls_back_to_full_mode(monkeypatch):
    """synthesize_segmentsがok:Falseを返したら全文方式へフォールバックする。"""
    project = _make_project_with_two_shots("断片TTS失敗テスト", [(0.0, 2.0), (0.0, 3.0)])
    project["narration_segments"] = {"s1": "断片1", "s2": "断片2"}
    projects.save_project(project)

    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: {"ok": False, "segments": [], "backend": None, "fallback_reason": "segment_1_exception:boom"},
    )
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())

    calls = _capture_ffmpeg(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "full"
    assert tts_meta["backend"] == "fake_full"
