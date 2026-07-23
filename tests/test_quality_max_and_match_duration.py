# -*- coding: utf-8 -*-
"""F11 --match-reference-duration と F12 supreme_plus プリセット（config.local 自動マージ）の単体テスト。"""
from __future__ import annotations

import json

import pytest

from pipeline import config as _cfg_mod
from pipeline import director


def test_config_local_json_files_are_deep_merged(tmp_path, monkeypatch):
    """`config.local/*.json` が config.json に deep_merge で上書きされる。"""
    # 一時プロジェクトルートを作る
    root = tmp_path
    (root / "config.json").write_text(
        json.dumps({
            "director_quality": "supreme",
            "director": {"min_shot_sec": None, "quality_directive": None},
            "reference": {"max_vision_calls": 4, "max_frames": 40},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    local_dir = root / "config.local"
    local_dir.mkdir()
    (local_dir / "quality_max.json").write_text(
        json.dumps({
            "director_quality": "supreme_plus",
            "director": {"min_shot_sec": 0.5, "quality_directive": "品質最優先"},
            "reference": {"max_vision_calls": 999},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    # config.py の PROJECT_ROOT / _CONFIG_PATH を一時ルートに向ける
    monkeypatch.setattr(_cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.setattr(_cfg_mod, "_CONFIG_PATH", root / "config.json")

    cfg = _cfg_mod.load_config()
    assert cfg["director_quality"] == "supreme_plus"
    assert cfg["director"]["min_shot_sec"] == 0.5
    assert cfg["director"]["quality_directive"] == "品質最優先"
    # deep merge: reference.max_vision_calls は上書き、max_frames は残る
    assert cfg["reference"]["max_vision_calls"] == 999
    assert cfg["reference"]["max_frames"] == 40


def test_config_local_absent_falls_back_to_config_json(tmp_path, monkeypatch):
    root = tmp_path
    (root / "config.json").write_text(
        json.dumps({"director_quality": "supreme"}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(_cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.setattr(_cfg_mod, "_CONFIG_PATH", root / "config.json")
    cfg = _cfg_mod.load_config()
    assert cfg["director_quality"] == "supreme"


def test_run_director_supports_supreme_plus_quality(monkeypatch, tmp_path):
    """quality='supreme_plus' が受理され、stages に rewrite が入る。"""
    import os
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")
    with open(fixture, "r", encoding="utf-8") as f:
        spec = json.load(f)

    def _fake_attempt(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                      trace=None, skeleton=None, reference=None):
        if trace is not None:
            trace["last_model_used"] = "test-model"
        if not skeleton:
            return None
        shots = []
        for s in skeleton["shots"]:
            shot = dict(s)
            shot["visual_prompt"] = "placeholder"
            shot["motion_preset"] = "static"
            shot["caption_jp"] = "テスト"
            shots.append(shot)
        return {
            "version": 2, "meta": {"source": "ai"},
            "concept": "c", "hook": "h", "narration_script": "n",
            "shots": shots, "bgm_mood": "upbeat",
            "sfx_plan": skeleton.get("sfx_plan") or [],
            "hook_end_shot_id": skeleton.get("hook_end_shot_id"),
            "cta_start_shot_id": skeleton.get("cta_start_shot_id"),
        }

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    plan = director.run_director(
        "theme",
        config={"director": {"min_shot_sec": 0.5, "quality_directive": "品質最優先: reference_visual を必ず英文に含めよ"}},
        target_duration_sec=30.0, reference=spec, quality="supreme_plus",
    )
    assert plan["meta"]["quality"] == "supreme_plus"
    stages = plan["meta"].get("stages") or {}
    assert "write" in stages
    assert "polish" in stages
    assert "rewrite" in stages, "supreme_plus は3段目 rewrite を実行する"


def test_quality_directive_is_injected_into_reference_block(monkeypatch):
    """config.director.quality_directive が reference_block の末尾に注入される。"""
    import os
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")
    with open(fixture, "r", encoding="utf-8") as f:
        spec = json.load(f)

    captured = {}

    def _fake_attempt(prompt, *args, **kw):
        captured.setdefault("prompt", prompt)
        return None  # 実行しない（プロンプトが目当て）

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    directive = "品質最優先マーカー_1234"
    with pytest.raises(director.TTPSkeletonMismatchError):
        director.run_director(
            "theme",
            config={"director": {"quality_directive": directive}},
            target_duration_sec=30.0, reference=spec, quality="single",
        )
    assert directive in captured["prompt"]


def test_cli_match_reference_duration_overrides_target(monkeypatch, tmp_path):
    """F11: run.py の --match-reference-duration が target を参考尺に強制一致させる。

    ここでは run_pipeline を直接呼び、reference_spec.duration_sec が target_duration_sec に
    上書きされることを検証する（reference stage の完了直後で上書きされる仕様）。
    """
    from pipeline import reference_v2

    def _fake_analyze(url, cfg=None, fetch_video=None, progress_cb=None):
        return {
            "ok": True, "cached": False, "warnings": [], "source": "test",
            "spec": {
                "version": 2, "url": url, "duration_sec": 47.5,
                "transcript": "", "segments": [], "beats": [], "rhythm": None,
                "cuts": [], "shots_ref": [], "telops": [], "sfx_events": [],
                "bgm": {"present": False, "mood_guess": ""},
                "warnings": [],
            },
        }

    # analyze だけをフェイクにし、director 以降で失敗させて report を早期に取得する。
    monkeypatch.setattr(reference_v2, "analyze_reference_v2", _fake_analyze)

    import run as run_mod

    def _boom(*args, **kw):
        raise RuntimeError("director stub")
    monkeypatch.setattr(run_mod.director, "run_director", _boom)

    report = run_mod.run_pipeline(
        theme="t", target_duration_sec=15.0, backend_name="mock", no_llm=False,
        cfg={"backend": "mock", "reference": {"cache_dir": str(tmp_path)}},
        quality="single", style="default",
        reference_url="https://example.com/x", match_reference_duration=True,
    )
    # F11 適用: target が 47.5 秒に上書きされ report に反映されている
    assert report["target_duration_sec"] == pytest.approx(47.5)
    assert report["stages"]["reference"].get("match_reference_duration") is True
