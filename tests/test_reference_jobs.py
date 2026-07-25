# -*- coding: utf-8 -*-
"""studio/server/jobs.py JobManager._run_generate の「参考動画リンク」ステージ単体テスト。

対象: reference_url が指定されたときのfail-open解析ステージ（product_urlのfail-open設計と
同型）。pipeline.reference.analyze_reference は別ワーカーが並行実装中のため、jobs.py側は
`from pipeline import reference` を遅延import + モジュール変数 `analyze_reference` として
公開している（jobs.py参照）。ここでは常に `jobs_mod.analyze_reference` をmonkeypatchし、
pipeline.reference の実装有無・実ネットワークに関わらず _run_generate の分岐ロジック
（成功/cached/失敗）だけを検証する。

ffmpeg/TTS等の重い実処理は行わない: ビジュアル生成は常に成功するモックバックエンドに
差し替え、render.run_ffmpeg・jobs.py内の _render_project（レンダリング本体）もモックして
高速に完走させる（test_jobs_generate.pyと同じ思想）。
"""
import shutil
from queue import Empty

import pytest

from pipeline import plan_schema
from studio.server import jobs as jobs_mod
from studio.server import projects


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _fake_run_director_factory(shot_count=2, capture=None):
    def _fake(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        if capture is not None:
            capture["target_duration_sec"] = target_duration_sec
            capture["reference"] = kwargs.get("reference")
        # T10: build_rule_based_plan -> build_smoke_plan (TTP v2 Phase 2)。
        return plan_schema.build_smoke_plan(
            theme, target_duration_sec=target_duration_sec or 15, shot_count=shot_count
        )
    return _fake


class _AlwaysSucceedingBackend:
    """全ショットの生成が必ず成功するビジュアルバックエンド（本テストの対象外部分を高速化する）。"""

    name = "mock"

    def __init__(self, cfg=None):
        pass

    def generate(self, shot, out_path):
        with open(out_path, "wb") as f:
            f.write(b"\x00")
        return {"backend": self.name}


def _make_job_manager_without_worker(monkeypatch):
    """バックグラウンドワーカースレッドを起動せず、_run_generateを同期的に呼べるJobManagerを作る。"""
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def _patch_happy_path_visual_and_render(monkeypatch):
    """ショット生成(ffmpeg正規化)とレンダリング本体をモックし、_run_generateがready到達まで
    高速に完走できるようにする（実際のffmpeg/TTS実行を避ける。参考動画ステージ自体はモックしない）。
    """
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: _AlwaysSucceedingBackend())
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    monkeypatch.setattr(
        jobs_mod, "_render_project",
        lambda project_id, plan, cfg: (
            "projects/{}/renders/fake.mp4".format(project_id), 15.0,
            {"backend": "mock", "duration_sec": 15.0, "is_silent": True},
        ),
    )


def _drain_stage_events(queue_obj):
    events = []
    while True:
        try:
            events.append(queue_obj.get_nowait())
        except Empty:
            break
    return events


def test_run_generate_skips_reference_analysis_when_reference_url_absent(monkeypatch):
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))

    called = {"n": 0}

    def _analyze(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("reference_url未指定なのにanalyze_referenceが呼ばれた")

    monkeypatch.setattr(jobs_mod, "analyze_reference", _analyze)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("参考動画なしテスト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "参考動画なしテスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
        })
        assert called["n"] == 0
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["reference"] is None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_reference_success_saves_meta_overrides_duration_and_passes_spec_to_director(monkeypatch):
    _patch_happy_path_visual_and_render(monkeypatch)
    capture = {}
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2, capture=capture))

    spec = {"duration_sec": 22.0, "beats": [{"role": "hook"}, {"role": "body"}]}
    reference_url = "https://www.tiktok.com/@example/video/123456"

    def _analyze(url, cfg, progress_cb=None):
        assert url == reference_url
        return {"ok": True, "spec": spec, "source": "fish_asr", "cached": False, "warnings": [], "error": None}

    monkeypatch.setattr(jobs_mod, "analyze_reference", _analyze)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("参考動画成功テスト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "参考動画成功テスト",
            "target_duration_sec": 15.0, "backend_name": "mock", "reference_url": reference_url,
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["reference"] == {
            "url": reference_url, "ok": True, "source": "fish_asr",
            "cached": False, "duration_sec": 22.0, "beats_count": 2, "warnings": [],
            # 声TTP: spec に narrator_voice が無ければ None（この spec には narration 推定なし）
            "narrator_voice": None,
            # R4 SFXゲート: この spec は sfx_events を持たない → SE無し判定 True
            "sfx_absent": True,
        }
        # 15<=22<=60なのでclampされず、そのままdirectorのtarget_duration_secへ反映される
        assert capture["target_duration_sec"] == 22.0
        assert capture["reference"] == spec
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


@pytest.mark.parametrize("raw_duration,expected", [(5.0, 15.0), (120.0, 60.0)])
def test_run_generate_reference_duration_override_is_clamped_15_to_60(monkeypatch, raw_duration, expected):
    _patch_happy_path_visual_and_render(monkeypatch)
    capture = {}
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2, capture=capture))
    spec = {"duration_sec": raw_duration, "beats": []}
    monkeypatch.setattr(jobs_mod, "analyze_reference", lambda url, cfg, progress_cb=None: {
        "ok": True, "spec": spec, "source": "vidiq", "cached": False, "warnings": [], "error": None,
    })

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("clampテスト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "clampテスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/{}".format(raw_duration),
        })
        assert capture["target_duration_sec"] == expected
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_reference_failure_fails_job_with_message(monkeypatch):
    """TTP v2 移行後、参考動画の解析失敗は fail-open ではなく明確な fail に切り替える
    (director 側で reference 無しの LLM 経路は例外になるため、ここで先に fail させて
    UI に「参考動画URLを見直す」誘導ができるようにする)。"""
    _patch_happy_path_visual_and_render(monkeypatch)
    capture = {}
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2, capture=capture))
    reference_url = "https://example.com/v/failure"
    monkeypatch.setattr(jobs_mod, "analyze_reference", lambda url, cfg, progress_cb=None: {
        "ok": False, "spec": None, "source": None, "cached": False, "warnings": [],
        "error": "動画を取得できませんでした",
    })

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("参考動画失敗テスト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "参考動画失敗テスト",
            "target_duration_sec": 15.0, "backend_name": "mock", "reference_url": reference_url,
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert saved["reference"] == {"url": reference_url, "ok": False, "error": "動画を取得できませんでした"}
        assert "動画を取得できませんでした" in (saved.get("error") or "")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_reference_analyze_exception_fails_job(monkeypatch):
    """analyze_reference 自体が例外を送出した場合も、TTP v2 では fail させる。"""
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))

    def _boom(url, cfg, progress_cb=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(jobs_mod, "analyze_reference", _boom)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("参考動画例外テスト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "参考動画例外テスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/exception",
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert saved["reference"]["ok"] is False
        assert "network down" in saved["reference"]["error"]
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_reference_cached_emits_reuse_message(monkeypatch):
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))
    spec = {"duration_sec": 20.0, "beats": []}
    monkeypatch.setattr(jobs_mod, "analyze_reference", lambda url, cfg, progress_cb=None: {
        "ok": True, "spec": spec, "source": "vidiq", "cached": True, "warnings": [], "error": None,
    })

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("キャッシュ利用テスト", 15.0, "mock", status="generating")
    q = manager.subscribe(project["id"])
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "キャッシュ利用テスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/cached",
        })
        events = _drain_stage_events(q)
        reference_messages = [e.get("message") for e in events if e.get("stage") == "reference"]
        assert "参考動画を解析中…" in reference_messages
        assert "解析結果を再利用します" in reference_messages
        saved = projects.get_project(project["id"])
        assert saved["reference"]["cached"] is True
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_reference_stage_precedes_director_stage_in_sse_order(monkeypatch):
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))
    spec = {"duration_sec": 20.0, "beats": []}
    monkeypatch.setattr(jobs_mod, "analyze_reference", lambda url, cfg, progress_cb=None: {
        "ok": True, "spec": spec, "source": "vidiq", "cached": False, "warnings": [], "error": None,
    })

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("SSE順序テスト", 15.0, "mock", status="generating")
    q = manager.subscribe(project["id"])
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "SSE順序テスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/order",
        })
        stages = [e.get("stage") for e in _drain_stage_events(q)]
        assert "reference" in stages
        assert "director" in stages
        assert stages.index("reference") < stages.index("director")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# TTP v2 移行: analyze_reference_v2 の progress_cb を jobs.py が受け取り、5段細分化された
# progress を emit すること。
# ---------------------------------------------------------------------------

def test_run_generate_reference_v2_emits_multistage_progress(monkeypatch):
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))
    spec = {"version": 2, "duration_sec": 20.0, "cuts": [], "sfx_events": [], "telops": []}

    def _analyze(url, cfg, progress_cb=None):
        # analyze_reference_v2 の 5段(download/cuts/vision/onsets/fusion)を疑似発火する。
        for stage in ("cache_check", "download", "detect_cuts", "extract_frames", "vision", "onsets", "asr", "fusion", "done"):
            if progress_cb is not None:
                progress_cb(stage)
        return {"ok": True, "spec": spec, "source": "multimodal", "cached": False, "warnings": [], "error": None}

    monkeypatch.setattr(jobs_mod, "analyze_reference", _analyze)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("v2進捗テスト", 15.0, "mock", status="generating")
    q = manager.subscribe(project["id"])
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "v2進捗テスト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/multistage",
        })
        events = _drain_stage_events(q)
        reference_messages = [e.get("message") for e in events if e.get("stage") == "reference"]
        # download/cuts/vision/onsets/fusion に相当する文言が emit されている
        assert any("ダウンロード" in m for m in reference_messages)
        assert any("カット" in m for m in reference_messages)
        assert any("vision" in m or "映像を解析" in m for m in reference_messages)
        assert any("オンセット" in m for m in reference_messages)
        assert any("統合解析" in m for m in reference_messages)
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_generate_rejects_v1_spec_with_guidance(monkeypatch):
    """analyze_reference が旧 v1 spec(version=1)を返したら案内文つきで fail する。"""
    _patch_happy_path_visual_and_render(monkeypatch)
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director_factory(2))
    v1_spec = {"version": 1, "duration_sec": 20.0, "beats": []}
    monkeypatch.setattr(jobs_mod, "analyze_reference", lambda url, cfg, progress_cb=None: {
        "ok": True, "spec": v1_spec, "source": "cache", "cached": True, "warnings": [], "error": None,
    })

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("v1リジェクト", 15.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], {
            "project_id": project["id"], "theme": "v1リジェクト",
            "target_duration_sec": 15.0, "backend_name": "mock",
            "reference_url": "https://example.com/v/old",
        })
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "旧バージョン" in (saved.get("error") or "")
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)
