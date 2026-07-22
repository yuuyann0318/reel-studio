# -*- coding: utf-8 -*-
"""TTP v2 SFX 経路の配線検証 (P5)。

`render.compute_edit_enhancement_kwargs` は plan=... を受けたときのみ
sfx_planner 経路 (plan.sfx_plan / hook_end_shot_id / cta_start_shot_id) を発動する。
本番の 2 呼び出し点:
  - run.py の CLI 経路 (Stage 7 render)
  - studio/server/jobs.py の _render_project

に対して、plan が実際に kwarg として渡っているかを spy で検証する。
"""
from pathlib import Path

import pytest

import run
from pipeline import compliance  # noqa: F401 (import 副作用回避)
from pipeline.config import load_config
from studio.server import jobs as jobs_mod
from studio.server import projects


# ---------------------------------------------------------------------------
# run.py CLI 経路: plan= が compute_edit_enhancement_kwargs へ渡ること
# ---------------------------------------------------------------------------

_SYNC_SHOTS = [
    {"id": "s1", "visual_prompt": "a", "motion_preset": "static", "duration_sec": 2.0,
     "caption_jp": "t1", "narration_jp": "断片1。"},
    {"id": "s2", "visual_prompt": "b", "motion_preset": "static", "duration_sec": 3.0,
     "caption_jp": "t2", "narration_jp": "断片2。"},
]


def _plan_v2_with_sfx_plan():
    return {
        "version": 2, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
        "narration_script": "断片1。断片2。",
        "shots": _SYNC_SHOTS,
        "bgm_mood": "upbeat",
        "sfx_plan": [
            {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0},
             "family": "whoosh"},
        ],
        "hook_end_shot_id": "s1",
        "cta_start_shot_id": "s2",
    }


def test_run_pipeline_passes_plan_to_compute_edit_enhancement_kwargs(monkeypatch):
    """run.py: Stage 7 render で compute_edit_enhancement_kwargs(plan=plan) が呼ばれる。"""
    cfg = load_config()
    fake_plan = _plan_v2_with_sfx_plan()

    monkeypatch.setattr(run.director, "run_director", lambda theme, config, **kw: fake_plan)

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 1.5},
        {"path": "/tmp/seg1.wav", "duration_sec": 2.5},
    ]

    def _fake_synthesize_segments(texts, out_dir, cfg_, voice="Kyoko"):
        return {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None}

    monkeypatch.setattr(run.tts_mod, "synthesize_segments", _fake_synthesize_segments)
    monkeypatch.setattr(run.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    captured = {}
    real_fn = run.render.compute_edit_enhancement_kwargs

    def _spy(durations, edit_profile, **kwargs):
        captured["plan"] = kwargs.get("plan")
        captured["project_seed"] = kwargs.get("project_seed")
        return real_fn(durations, edit_profile, **kwargs)

    monkeypatch.setattr(run.render, "compute_edit_enhancement_kwargs", _spy)

    run.run_pipeline("plan配線CLIテスト", 5.0, "mock", True, cfg, quality="single", style="default")

    assert captured.get("plan") is not None, "plan が compute_edit_enhancement_kwargs に渡っていない"
    assert captured["plan"].get("sfx_plan"), captured["plan"]
    # plan.sfx_plan がある + sfx_planner 経路が発動したことは resolve_hook_cta_bounds の
    # 結果（hook_end_sec = s1 の終端 = durations[0]）で確認する。
    from pipeline import sfx_planner
    hook_end, cta_start = sfx_planner.resolve_hook_cta_bounds(
        captured["plan"], [1.75, 2.75],  # display=1.5+0.25 と 2.5+0.25 相当
    )
    assert hook_end is not None
    assert cta_start is not None


# ---------------------------------------------------------------------------
# studio/server/jobs.py: _render_project 経路
# ---------------------------------------------------------------------------

class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 4.0, "is_silent": False}


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_studio_project_with_plan_v2_extras():
    """Studio 内部 plan は sfx_plan 等をトップレベルにも持たせられる（純粋な dict なので）。
    validate_plan は normalize で剥がすが、_render_project は project.plan (dict) を受け取り
    そのまま kwargs=plan で compute_edit_enhancement_kwargs に渡す設計。"""
    project = projects.create_project("plan配線 studio", 5.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for idx, (start, end) in enumerate([(0.0, 2.0), (0.0, 3.0)], start=1):
        sid = "s{}".format(idx)
        clip_path = clips_dir / "{}.mp4".format(sid)
        clip_path.write_bytes(b"\x00")
        shots.append({
            "id": sid, "order": idx - 1, "enabled": True, "prompt": "abstract",
            "caption": "テスト{}".format(idx),
            "clip_path": projects.media_relpath_for_clip(project["id"], "{}.mp4".format(sid)),
            "source_duration": end, "trim": {"start": start, "end": end},
        })
    project["plan"] = {
        "shots": shots, "narration_text": "全文テストナレーション。",
        "bgm": None, "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
        # v2 相当のトップレベル拡張。_render_project は plan dict をそのまま
        # compute_edit_enhancement_kwargs(plan=plan) に渡すため、この 3 キーが
        # sfx_planner 経路の発動条件になる。
        "sfx_plan": [
            {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0},
             "family": "whoosh"},
        ],
        "hook_end_shot_id": "s1",
        "cta_start_shot_id": "s2",
    }
    projects.save_project(project)
    return project


def test_render_project_passes_plan_to_compute_edit_enhancement_kwargs(monkeypatch):
    """studio jobs._render_project: compute_edit_enhancement_kwargs(plan=plan) が呼ばれる。"""
    project = _make_studio_project_with_plan_v2_extras()

    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full modeで呼ばれてはいけない")),
    )
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend",
                         lambda voice="Kyoko", cfg=None: _FakeFullBackend())
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg",
                         lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    captured = {}
    real_fn = jobs_mod.render.compute_edit_enhancement_kwargs

    def _spy(durations, edit_profile, **kwargs):
        captured["plan"] = kwargs.get("plan")
        captured["project_seed"] = kwargs.get("project_seed")
        return real_fn(durations, edit_profile, **kwargs)

    monkeypatch.setattr(jobs_mod.render, "compute_edit_enhancement_kwargs", _spy)

    cfg = {"ffmpeg_bin": "/bin/ffmpeg"}
    jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert captured.get("plan") is not None, "plan が compute_edit_enhancement_kwargs に渡っていない"
    assert captured["plan"].get("sfx_plan"), "plan.sfx_plan がそのまま届いている必要がある"
    assert captured["plan"].get("hook_end_shot_id") == "s1"
    assert captured["plan"].get("cta_start_shot_id") == "s2"
