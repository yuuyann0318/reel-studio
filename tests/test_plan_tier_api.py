# -*- coding: utf-8 -*-
"""プラン(free/paid)の API 経路テスト: POST /api/estimate と POST /api/projects の plan_tier 配線。

directorの実行（claude 呼び出し）を避けるため start_generate を monkeypatch し、endpoint の
plan_tier→backend 解決・billing 記録・project.json 永続化のみを検証する。
"""
import pytest
from fastapi.testclient import TestClient

from studio.server.app import app
from studio.server import app as app_mod
from studio.server import projects

client = TestClient(app)

_DUMMY_REFERENCE_URL = "https://www.tiktok.com/@example/video/123"


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def test_estimate_free_is_zero():
    resp = client.post("/api/estimate", json={"plan_tier": "free", "duration": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_tier"] == "free"
    assert body["coins"] == 0
    assert body["approximate"] is False


def test_estimate_rejects_invalid_or_missing_tier():
    # codex-review P2: typo/未指定を黙って無料0コインで返さない。
    r1 = client.post("/api/estimate", json={"plan_tier": "fre", "duration": 30})
    assert r1.status_code == 400
    assert r1.json()["error"]["code"] == "invalid_plan_tier"
    r2 = client.post("/api/estimate", json={"duration": 30})
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "invalid_plan_tier"


def test_create_project_rejects_invalid_plan_tier(monkeypatch):
    # codex-review P1: 非空だが free/paid でない値は 400（黙って backend フォールバックさせない）。
    _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:typo", "plan_tier": "fre",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_plan_tier"


def test_create_project_backend_only_paid_records_paid_billing(monkeypatch):
    # codex-review P1: plan_tier 未指定 + backend=higgsfield は billing も paid で記録する
    # （無料0コインの取り違え防止）。
    _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:後方互換paid", "backend": "higgsfield",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202
    pid = resp.json()["id"]
    proj = projects.get_project(pid)
    assert proj["backend"] == "higgsfield"
    assert proj["billing"]["plan_tier"] == "paid"
    assert proj["billing"]["coins_estimated"] > 0


def test_estimate_paid_is_positive_and_approximate():
    resp = client.post("/api/estimate", json={"plan_tier": "paid", "duration": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_tier"] == "paid"
    assert body["coins"] > 0
    assert body["approximate"] is True
    assert "コイン" in body["note"]


def test_estimate_does_not_call_higgsfield_or_fish(monkeypatch):
    # 見積エンドポイントは実バックエンドを構築しないこと（get_backend を呼ばない）。
    import pipeline.visual as visual_mod

    def _boom(*a, **k):
        raise AssertionError("estimate must not construct a visual backend")

    monkeypatch.setattr(visual_mod, "get_backend", _boom)
    resp = client.post("/api/estimate", json={"plan_tier": "paid", "duration": 30})
    assert resp.status_code == 200


def _patch_start_generate(monkeypatch):
    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None, plan_tier=None, **_kwargs):
        captured["backend_name"] = backend_name
        captured["plan_tier"] = plan_tier
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)
    return captured


def test_create_project_free_persists_mock_backend_and_zero_estimate(monkeypatch):
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:むりょうコース", "plan_tier": "free",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202
    pid = resp.json()["id"]
    assert captured["plan_tier"] == "free"
    assert captured["backend_name"] == "mock"
    proj = projects.get_project(pid)
    assert proj["plan_tier"] == "free"
    assert proj["backend"] == "mock"
    assert proj["billing"]["coins_estimated"] == 0
    assert proj["billing"]["plan_tier"] == "free"


def test_create_project_paid_forces_higgsfield_and_positive_estimate(monkeypatch):
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:本番コース", "plan_tier": "paid",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202
    pid = resp.json()["id"]
    assert captured["plan_tier"] == "paid"
    assert captured["backend_name"] == "higgsfield"
    proj = projects.get_project(pid)
    assert proj["plan_tier"] == "paid"
    assert proj["backend"] == "higgsfield"
    assert proj["billing"]["coins_estimated"] > 0


def test_create_project_plan_tier_overrides_conflicting_backend(monkeypatch):
    # plan_tier=free と backend=higgsfield が同時に来ても free が勝つ（UI 撤去済みだが後方互換の保険）。
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:矛盾", "plan_tier": "free", "backend": "higgsfield",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202
    assert captured["backend_name"] == "mock"


def test_create_project_backward_compat_backend_only(monkeypatch):
    # plan_tier 未指定なら従来どおり backend を尊重する。
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:後方互換", "backend": "mock",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202
    pid = resp.json()["id"]
    assert captured["plan_tier"] is None
    assert captured["backend_name"] == "mock"
    proj = projects.get_project(pid)
    # 表示用に billing.plan_tier は backend から推定される（mock→free）。
    assert proj["billing"]["plan_tier"] == "free"
