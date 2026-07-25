# -*- coding: utf-8 -*-
"""GET /api/health: claude 実疎通 + ffmpeg/ffprobe/yt-dlp の3点検査。

claude 疎通と CLI 疎通はいずれも monkeypatch でスタブ化して課金ゼロ・決定論的に検証する
（実 CLI は起動が重く flaky なため、実バイナリ疎通は @slow の別テストで1回だけ確認する）。
"""
import pytest
from fastapi.testclient import TestClient

import studio.server.app as app_mod
from studio.server.app import app

client = TestClient(app)


def _stub_clis(monkeypatch, ok=True):
    monkeypatch.setattr(
        app_mod, "_check_cli",
        lambda bin_path, args, timeout_sec=30: {"ok": ok, "detail": "stub"},
    )


def _stub_claude(monkeypatch, ok=True):
    monkeypatch.setattr(
        "pipeline.claude_runner.probe_claude",
        lambda *a, **k: {"ok": ok, "detail": "ok" if ok else "認証に失敗",
                         "category": None if ok else "unreachable",
                         "model_used": "claude-opus-4-8" if ok else None},
    )


def test_health_all_ok(monkeypatch):
    _stub_clis(monkeypatch, ok=True)
    _stub_claude(monkeypatch, ok=True)
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["ok"] is True
    assert body["checks"]["claude"]["ok"] is True
    for tool in ("ffmpeg", "ffprobe", "yt_dlp"):
        assert body["checks"][tool]["ok"] is True, body["checks"][tool]


def test_health_claude_ng_returns_503(monkeypatch):
    _stub_clis(monkeypatch, ok=True)
    _stub_claude(monkeypatch, ok=False)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["checks"]["claude"]["ok"] is False


def test_health_cli_ng_returns_503(monkeypatch):
    _stub_clis(monkeypatch, ok=False)
    _stub_claude(monkeypatch, ok=True)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_health_skip_claude_is_cheap(monkeypatch):
    # ?claude=0 のときは probe_claude を一切呼ばない（課金ゼロの軽量チェック）。
    _stub_clis(monkeypatch, ok=True)
    called = {"n": 0}
    monkeypatch.setattr(
        "pipeline.claude_runner.probe_claude",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"ok": True},
    )
    resp = client.get("/api/health?claude=0")
    body = resp.json()
    assert "claude" not in body["checks"]
    assert called["n"] == 0


def test_compute_health_check_claude_false_skips_probe(monkeypatch):
    _stub_clis(monkeypatch, ok=True)
    called = {"n": 0}
    monkeypatch.setattr(
        "pipeline.claude_runner.probe_claude",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"ok": True},
    )
    res = app_mod.compute_health(check_claude=False)
    assert "claude" not in res["checks"]
    assert called["n"] == 0
    assert res["checks"]["ffmpeg"]["ok"] is True


@pytest.mark.slow
def test_health_real_binaries_smoke():
    """同梱 bin/ffmpeg・ffprobe・yt-dlp が実際に --version で疎通することを1回だけ確認する。"""
    res = app_mod.compute_health(check_claude=False)
    for tool in ("ffmpeg", "ffprobe", "yt_dlp"):
        assert res["checks"][tool]["ok"] is True, res["checks"][tool]
