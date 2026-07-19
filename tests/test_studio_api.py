# -*- coding: utf-8 -*-
"""Reel Studio: FastAPI エンドポイントの単体テスト（TestClient）+ 実機E2E（mockバックエンド）。

実機E2E（test_generate_edit_render_e2e）は director.run_director の claude CLI 呼び出しを
monkeypatchでno_llm経路（決定論的テンプレート生成）に差し替え、外部API課金を発生させない
（tests/test_run.pyの既存の重いテストと同じ思想）。ffmpeg/TTSは実行するため@pytest.mark.slow。
"""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

import pipeline.director as director_mod
from studio.server.app import app
from studio.server import projects

client = TestClient(app)


def test_list_projects_returns_list():
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_unknown_project_returns_404_with_error_shape():
    resp = client.get("/api/projects/p_does_not_exist_xyz")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert "message" in body["error"]


def test_create_project_requires_theme():
    resp = client.post("/api/projects", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_theme"


def test_create_project_rejects_invalid_backend():
    resp = client.post("/api/projects", json={"theme": "テスト", "backend": "not_a_backend"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_backend"


def test_create_project_rejects_invalid_style():
    resp = client.post("/api/projects", json={"theme": "テスト", "style": "not_a_real_style"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_style"


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://example.com/product",
        "javascript:alert(1)",
        "",
        "   ",
        123,
    ],
)
def test_create_project_rejects_invalid_product_url(bad_url):
    resp = client.post("/api/projects", json={"theme": "テスト", "backend": "mock", "product_url": bad_url})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_product_url"


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://example.com/video",
        "javascript:alert(1)",
        "",
        "   ",
        123,
    ],
)
def test_create_project_rejects_invalid_reference_url(bad_url):
    resp = client.post("/api/projects", json={"theme": "テスト", "backend": "mock", "reference_url": bad_url})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_reference_url"


def test_create_project_omits_product_url_when_not_provided(monkeypatch):
    """product_urlを指定しない従来どおりの呼び出しは、job_manager.start_generateへ
    product_url=Noneで渡り、既存の非商品モードの挙動を壊さないこと。"""
    from studio.server import app as app_mod

    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None):
        captured["product_url"] = product_url
        captured["reference_url"] = reference_url
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)

    resp = client.post("/api/projects", json={"theme": "テスト:商品URL無し", "backend": "mock"})
    assert resp.status_code == 202
    project_id = resp.json()["id"]
    try:
        assert captured["product_url"] is None
        assert captured["reference_url"] is None
    finally:
        shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)


def test_create_project_threads_style_into_start_generate(monkeypatch):
    """POST /api/projects の style が job_manager.start_generate に渡ることを検証する。

    directorの実行（claude呼び出し）を避けるため、start_generate自体をmonkeypatchして
    実際のジョブは起動させず、呼び出し引数だけを確認する。
    """
    from studio.server import app as app_mod

    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None):
        captured["project_id"] = project_id
        captured["style"] = style
        captured["product_url"] = product_url
        captured["reference_url"] = reference_url
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)

    resp = client.post("/api/projects", json={"theme": "テスト:style疎通", "backend": "mock", "style": "vertical_hook"})
    assert resp.status_code == 202
    project_id = resp.json()["id"]
    try:
        assert captured["style"] == "vertical_hook"
        assert captured["project_id"] == project_id
    finally:
        shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)


def test_create_project_threads_product_url_into_start_generate_and_persists_none_before_job_runs(monkeypatch):
    """POST /api/projects の product_url が job_manager.start_generate に渡ること、
    かつジョブ完了前（202直後）は project["product"] が None のまま保存されていること
    （実際の商品情報/画像はジョブが collect_product_images 実行後に書き込む）。"""
    from studio.server import app as app_mod

    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None):
        captured["project_id"] = project_id
        captured["product_url"] = product_url
        captured["reference_url"] = reference_url
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)

    product_url = "https://shop.example.com/item/42"
    resp = client.post("/api/projects", json={"theme": "テスト:商品URL疎通", "backend": "mock", "product_url": product_url})
    assert resp.status_code == 202
    project_id = resp.json()["id"]
    try:
        assert captured["product_url"] == product_url
        assert captured["reference_url"] is None
        fetched = client.get("/api/projects/{}".format(project_id)).json()
        assert fetched["product"] is None
    finally:
        shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)


def test_create_project_threads_reference_url_into_start_generate_and_persists_none_before_job_runs(monkeypatch):
    """POST /api/projects の reference_url が job_manager.start_generate に渡ること、
    かつジョブ完了前（202直後）は project["reference"] が None のまま保存されていること
    （実際の解析結果はジョブが analyze_reference 実行後に書き込む）。"""
    from studio.server import app as app_mod

    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None):
        captured["project_id"] = project_id
        captured["reference_url"] = reference_url
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)

    reference_url = "https://www.tiktok.com/@example/video/123456"
    resp = client.post(
        "/api/projects", json={"theme": "テスト:参考動画URL疎通", "backend": "mock", "reference_url": reference_url}
    )
    assert resp.status_code == 202
    project_id = resp.json()["id"]
    try:
        assert captured["reference_url"] == reference_url
        fetched = client.get("/api/projects/{}".format(project_id)).json()
        assert fetched["reference"] is None
    finally:
        shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)


def test_create_project_accepts_vertical_hook_style():
    # directorのclaude呼び出し（課金/低速）を実行しないよう、POST直後に同期的に書き込まれる
    # project["style"]/plan.subtitle_style.presetのみを検証する（バックグラウンドの生成ジョブ
    # 完了は待たない。project.create_project()がジョブ開始前にこれらを書くため検証できる）。
    project = projects.create_project("テスト:vertical_hook作成", 15.0, "mock", status="draft", style="vertical_hook")
    try:
        assert project["style"] == "vertical_hook"
        assert project["plan"]["subtitle_style"]["preset"] == "vertical_hook"
        fetched = client.get("/api/projects/{}".format(project["id"])).json()
        assert fetched["style"] == "vertical_hook"
        assert fetched["plan"]["subtitle_style"]["preset"] == "vertical_hook"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_put_plan_unknown_project_returns_404():
    resp = client.put("/api/projects/p_does_not_exist_xyz/plan", json={"shots": []})
    assert resp.status_code == 404


def test_render_unknown_project_returns_404():
    resp = client.post("/api/projects/p_does_not_exist_xyz/render")
    assert resp.status_code == 404


def test_job_events_unknown_job_id_reports_not_found_in_stream():
    with client.stream("GET", "/api/jobs/job_does_not_exist/events") as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line]
    assert lines
    payload = json.loads(lines[0][len("data: "):])
    assert payload["error"]["code"] == "not_found"


def test_list_bgm_assets_has_three_moods():
    resp = client.get("/api/assets/bgm")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert {"upbeat", "calm", "emotional"} == {e["mood"] for e in data}


def test_list_sfx_assets_has_eight_effects_with_license_and_duration():
    resp = client.get("/api/assets/sfx")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 8
    for entry in data:
        assert 0.3 <= entry["duration"] <= 1.5
        assert entry["license"].startswith("synthetic-placeholder")


def test_media_bgm_mount_matches_frontend_media_url_scheme():
    # studio/web/api.js の mediaUrl("bgm", file) は "/media/bgm/<file>" を叩く契約
    resp = client.get("/media/bgm/upbeat_01.mp3")
    assert resp.status_code == 200


def test_media_sfx_mount_matches_frontend_media_url_scheme():
    # studio/web/api.js の mediaUrl("sfx", file) は "/media/sfx/<file>" を叩く契約
    resp = client.get("/media/sfx/whoosh_01.wav")
    assert resp.status_code == 200


def test_render_conflict_returns_409_while_status_is_rendering():
    project = projects.create_project("競合テスト", 10.0, "mock", status="rendering")
    try:
        resp = client.post("/api/projects/{}/render".format(project["id"]))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# BUG-invalid_plan回帰: 生成途中失敗（一部ショットのclip_path=None）でも保存できること、
# かつrender開始時は未生成ショットをshot id付き400で拒否すること
# ---------------------------------------------------------------------------

def _put_plan_with_unrendered_shot(project_id, extra_shot_overrides=None):
    project = projects.get_project(project_id)
    plan = project["plan"]
    shot = {
        "id": "s11", "order": 0, "enabled": True, "prompt": "abstract", "caption": "未生成キャプション",
        "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
    }
    if extra_shot_overrides:
        shot.update(extra_shot_overrides)
    plan["shots"] = [shot]
    return client.put("/api/projects/{}/plan".format(project_id), json=plan)


def test_put_plan_saves_successfully_with_unrendered_shot_clip_path_null():
    project = projects.create_project("途中失敗テスト", 10.0, "mock", status="failed")
    try:
        resp = _put_plan_with_unrendered_shot(project["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan"]["shots"][0]["clip_path"] is None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# 回帰: PUT /api/projects/{id}/plan は薬機法NGワード検査をすり抜けていた
# （商品アフィリエイト動画モードのプロジェクトでも、render開始/generate時と違い
#  compliance.BEAUTY_YAKKIHO_NG_WORDS/PATTERNS が合成されずに保存できてしまっていた）
# ---------------------------------------------------------------------------

def _plan_with_caption(project, caption):
    plan = project["plan"]
    plan["shots"] = [{
        "id": "s1", "order": 0, "enabled": True, "prompt": "abstract",
        "caption": caption,
        "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
    }]
    return plan


def test_put_plan_rejects_yakkiho_ng_caption_when_project_has_product():
    project = projects.create_project("商品モード薬機法テスト", 10.0, "mock", status="draft")
    project["product"] = {"name": "美容液X", "url": "https://shop.example.com/lp", "images": [], "warnings": []}
    projects.save_project(project)
    try:
        plan = _plan_with_caption(project, "このクリームで頑固なシミが消える")
        resp = client.put("/api/projects/{}/plan".format(project["id"]), json=plan)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_plan"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_put_plan_allows_yakkiho_words_when_project_has_no_product():
    """通常モード（project["product"] is None）は従来どおり薬機法NGワードの合成を行わず
    （挙動不変）、DEFAULT_NG_WORDSにのみ該当しなければ保存できること。"""
    project = projects.create_project("通常モードテスト", 10.0, "mock", status="draft")
    assert project["product"] is None
    try:
        plan = _plan_with_caption(project, "このクリームで頑固なシミが消える")
        resp = client.put("/api/projects/{}/plan".format(project["id"]), json=plan)
        assert resp.status_code == 200, resp.text
        assert resp.json()["plan"]["shots"][0]["caption"] == "このクリームで頑固なシミが消える"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_render_rejects_unrendered_enabled_shots_with_400_listing_shot_ids():
    project = projects.create_project("未生成render拒否テスト", 10.0, "mock", status="draft")
    try:
        put_resp = _put_plan_with_unrendered_shot(project["id"])
        assert put_resp.status_code == 200, put_resp.text

        render_resp = client.post("/api/projects/{}/render".format(project["id"]))
        assert render_resp.status_code == 400
        body = render_resp.json()
        assert body["error"]["code"] == "unrendered_shots"
        assert "s11" in body["error"]["message"]

        # ステータスは "rendering" に遷移させず、再送信を妨げない
        reloaded = projects.get_project(project["id"])
        assert reloaded["status"] != "rendering"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_render_ignores_unrendered_shot_when_disabled():
    project = projects.create_project("無効ショットは無視テスト", 10.0, "mock", status="draft")
    try:
        put_resp = _put_plan_with_unrendered_shot(project["id"], extra_shot_overrides={"enabled": False})
        assert put_resp.status_code == 200, put_resp.text

        render_resp = client.post("/api/projects/{}/render".format(project["id"]))
        # 有効ショットが1本もないため別のエラー（unrendered_shots以外）にはなるが、
        # 少なくとも「無効ショットの未生成」を理由にした400にはならないことを確認する。
        if render_resp.status_code == 400:
            assert render_resp.json()["error"]["code"] != "unrendered_shots"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def _drain_sse_until_done(path, timeout_sec=120):
    """SSEストリームを読み、`{"done":true,...}` イベントが来るまでブロックして返す。"""
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


@pytest.mark.slow
def test_generate_edit_render_e2e(monkeypatch):
    """mockバックエンドでの実機E2E: 生成→SSE完走→plan編集(カット1・SFX2発)→render→SSE完走。"""

    _original_run_director = director_mod.run_director

    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        # claude CLI(課金/低速)を使わず、決定論的テンプレートで即座に生成する。
        return _original_run_director(theme, cfg, target_duration_sec=target_duration_sec, no_llm=True)

    monkeypatch.setattr(director_mod, "run_director", _fake_run_director)

    resp = client.post("/api/projects", json={"theme": "テストE2Eテーマ", "duration": 6, "backend": "mock"})
    assert resp.status_code == 202
    project_id = resp.json()["id"]

    try:
        final_event = _drain_sse_until_done("/api/jobs/{}/events".format(project_id))
        assert final_event["ok"] is True, final_event

        project = client.get("/api/projects/{}".format(project_id)).json()
        assert project["status"] == "ready"
        shots = project["plan"]["shots"]
        assert len(shots) >= 2
        assert len(project["renders"]) == 1
        assert shots[0]["clip_path"].startswith("projects/{}/clips/".format(project_id))
        clip_media_resp = client.get("/media/" + shots[0]["clip_path"])
        assert clip_media_resp.status_code == 200

        plan = project["plan"]
        plan["shots"][0]["enabled"] = False  # 1ショットをカット
        plan["sfx"] = [
            {"file": "whoosh_01.wav", "at_sec": 0.2, "gain_db": -6},
            {"file": "ding_01.wav", "at_sec": 1.0, "gain_db": -8},
        ]
        put_resp = client.put("/api/projects/{}/plan".format(project_id), json=plan)
        assert put_resp.status_code == 200, put_resp.text

        render_resp = client.post("/api/projects/{}/render".format(project_id))
        assert render_resp.status_code == 202
        job_id = render_resp.json()["job_id"]
        assert job_id != project_id

        final_event2 = _drain_sse_until_done("/api/jobs/{}/events".format(job_id))
        assert final_event2["ok"] is True, final_event2

        project2 = client.get("/api/projects/{}".format(project_id)).json()
        assert project2["status"] == "ready"
        assert len(project2["renders"]) == 2
        out_rel_path = project2["renders"][-1]["path"]
        assert out_rel_path.startswith("projects/{}/renders/".format(project_id))
        out_abs_path = projects.resolve_media_relpath(out_rel_path)
        assert out_abs_path.exists()
        assert out_abs_path.stat().st_size > 0

        # フロント側 mediaUrl("raw", file) 契約: "/media/" + file が実際に配信されること
        media_resp = client.get("/media/" + out_rel_path)
        assert media_resp.status_code == 200
    finally:
        shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)
