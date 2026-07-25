# -*- coding: utf-8 -*-
"""商品アフィリエイト動画モード（product_url）のStudio統合テスト。

実装済み部品（pipeline/tts.py・pipeline/product_images.py・pipeline/compliance.py・
pipeline/director.py・visual backends）は編集禁止のため直接は変更しない。ここでは
studio/server/jobs.py がこれらをどう配線しているかを検証する。director.run_director・
product_images.collect_product_images・ビジュアルbackend・_render_project はすべて
monkeypatchでスタブ化し、実ネットワーク/実Higgsfield/実Fish Audio呼び出しは一切行わない。

PROJECTS_ROOTをtmp_pathへ差し替えて実プロジェクトディレクトリを汚さない
（tests/test_jobs_generate.py・tests/test_resume_and_recovery.py と同じ手法）。
"""
import os
import shutil

import pytest

from studio.server import jobs as jobs_mod
from studio.server import projects

PRODUCT_URL = "https://shop.example.com/item/1"


@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_job_manager_without_worker(monkeypatch):
    monkeypatch.setattr(jobs_mod.threading.Thread, "start", lambda self: None)
    return jobs_mod.JobManager()


def _plan_with_shots(shot_captions):
    """jobs.py が期待するdirector plan形状（plan_schema.pyのスキーマ）を手組みする。"""
    shots = []
    for i, caption in enumerate(shot_captions):
        shots.append({
            "id": "s{}".format(i + 1),
            "visual_prompt": "abstract product shot {}".format(i + 1),
            "motion_preset": "static",
            "duration_sec": 3.0,
            "caption_jp": caption,
        })
    return {
        "version": 1,
        "meta": {"source": "rule", "model_used": None, "quality": "single"},
        "concept": "テスト企画",
        "hook": shot_captions[0] if shot_captions else "",
        "narration_script": "".join(shot_captions),
        "shots": shots,
        "bgm_mood": "none",
    }


class _RecordingBackend:
    """呼ばれたshotをそのまま記録する（image_pathがbackendまで届くかを検証するため）。"""

    name = "mock"

    def __init__(self, cfg=None):
        self.shots = []

    def generate(self, shot, out_path):
        self.shots.append(dict(shot))
        with open(out_path, "wb") as f:
            f.write(b"\x00")
        return {"backend": self.name}


def _stub_render_project(rel_name="out.mp4", duration=12.3, tts_meta=None):
    """_render_project をスタブ化する（実ffmpeg/実TTSを一切実行しない）。"""
    tts_meta = tts_meta or {
        "backend": "say", "duration_sec": duration, "is_silent": False,
        "requested_backend": "fish_audio", "fallback_reason": "api_key_missing",
    }

    def _fake(project_id, plan, cfg):
        return projects.media_relpath_for_render(project_id, rel_name), duration, tts_meta

    return _fake, tts_meta


def _generate_payload(project_id, theme, product_url=None):
    return {
        "project_id": project_id, "theme": theme, "target_duration_sec": 12.0,
        "backend_name": "mock", "style": "default", "product_url": product_url,
    }


# ---------------------------------------------------------------------------
# (c) collect_product_images をmonkeypatchした生成フローで、shotにimage_pathが
#     割り当たり、backend.generateまで届くこと
# ---------------------------------------------------------------------------

def test_product_url_fetches_images_assigns_to_hook_and_cta_shots_and_reaches_backend(monkeypatch):
    captured_director_kwargs = {}

    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        captured_director_kwargs.update(kwargs)
        return _plan_with_shots(["フック文言", "中間1", "中間2", "CTA文言"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    captured_collect_args = {}

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        captured_collect_args.update({"url": url, "dest_dir": dest_dir, "cfg": cfg, "local_dir": local_dir})
        return {
            "name": "テスト商品A",
            "url": url,
            "images": [
                {"path": os.path.join(dest_dir, "001.jpg"), "source": "lp", "width": 800, "height": 800},
                {"path": os.path.join(dest_dir, "002.jpg"), "source": "lp", "width": 800, "height": 800},
            ],
            "warnings": [],
        }

    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    # 本テストは「割当プラミング（フック/CTAへの image_path 付与）」を検証する。分類（vision＋
    # 決定論プレフィルタ）は実 claude/ffmpeg に依存し、かつ fake パスは実在しないため、ここでは
    # 分類を全採用で固定して割当ロジックだけを純粋に検証する。
    def _fake_classify(paths, **kwargs):
        return [
            {"path": p, "category": "product_solo", "sharpness": "high",
             "dominant_colors": [], "adopted": True, "reason": None}
            for p in paths
        ]

    monkeypatch.setattr(jobs_mod.product_images, "classify_product_images", _fake_classify)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})

    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("商品モードE2E", 12.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "商品モードE2E", product_url=PRODUCT_URL))

        # directorはcollect_product_imagesの結果から作った商品情報を受け取っている
        assert captured_director_kwargs["product"] == {"name": "テスト商品A", "url": PRODUCT_URL, "image_count": 2}

        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["product"]["name"] == "テスト商品A"
        assert saved["product"]["url"] == PRODUCT_URL
        assert len(saved["product"]["images"]) == 2
        for img in saved["product"]["images"]:
            assert img["path"].startswith("projects/{}/product/".format(project["id"]))
        assert saved["tts"] == tts_meta

        # 4ショット・2画像: フック(先頭)とCTA(末尾)に割り当て、中間2枚には割当なし
        expected_hook = os.path.join(captured_collect_args["dest_dir"], "001.jpg")
        expected_cta = os.path.join(captured_collect_args["dest_dir"], "002.jpg")
        plan_shots = saved["plan"]["shots"]
        assert plan_shots[0]["image_path"] == expected_hook
        assert plan_shots[-1]["image_path"] == expected_cta
        assert "image_path" not in plan_shots[1]
        assert "image_path" not in plan_shots[2]

        # backend.generate() に渡されたshotにも image_path が含まれている
        backend_by_id = {s["id"]: s for s in backend.shots}
        assert backend_by_id["s1"]["image_path"] == expected_hook
        assert backend_by_id["s4"]["image_path"] == expected_cta
        assert "image_path" not in backend_by_id["s2"]
        assert "image_path" not in backend_by_id["s3"]
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (d) 画像0枚/取得失敗でもfailせず、warningsを通知して通常モードで続行すること
# ---------------------------------------------------------------------------

def test_product_image_fetch_failure_does_not_fail_job_and_falls_back_to_normal_mode(monkeypatch):
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編", "CTA"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    def _fake_collect_fails(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        return {"name": "", "url": url, "images": [], "warnings": ["商品LPの取得に失敗しました: timeout"]}

    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect_fails)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("商品画像取得失敗テスト", 12.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        manager._run_generate(
            project["id"], _generate_payload(project["id"], "商品画像取得失敗テスト", product_url=PRODUCT_URL)
        )

        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"  # 画像0枚/取得失敗でもfailしない
        assert saved["product"]["images"] == []
        assert saved["product"]["warnings"] == ["商品LPの取得に失敗しました: timeout"]
        assert all("image_path" not in s for s in saved["plan"]["shots"])
        assert all("image_path" not in s for s in backend.shots)
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (e) productモードのみ薬機法検査が効くこと（generate経路・render経路の両方）
# ---------------------------------------------------------------------------

def test_product_mode_blocks_yakkiho_ng_words(monkeypatch):
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["このシミが消える美容液です", "本編", "今すぐチェック"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        return {"name": "テスト商品", "url": url, "images": [], "warnings": []}

    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("薬機法ブロックテスト", 10.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "薬機法ブロックテスト", product_url=PRODUCT_URL))
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "コンプライアンス違反" in saved["error"]
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_normal_mode_does_not_check_yakkiho_ng_words(monkeypatch):
    """product_url無し（通常モード）では薬機法ワードは検査対象外で、生成が続行されること。"""
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["このシミが消える美容液です", "本編", "今すぐチェック"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("通常モード薬機法非対象テスト", 10.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "通常モード薬機法非対象テスト", product_url=None))
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"  # 薬機法ワードは通常モードでは検査対象外なのでブロックされない
        assert saved["product"] is None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_render_blocks_yakkiho_words_when_project_has_product(monkeypatch):
    project = projects.create_project("render時薬機法ブロックテスト", 10.0, "mock", status="draft")
    project["product"] = {"name": "テスト商品", "url": PRODUCT_URL, "images": [], "warnings": []}
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "s1.mp4").write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract",
            "caption": "このシミが消える美容液です",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }],
        "narration_text": "", "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_render("job_render_yakkiho_test", {"project_id": project["id"]})
        saved = projects.get_project(project["id"])
        assert saved["status"] == "failed"
        assert "コンプライアンス違反" in saved["error"]
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_render_does_not_block_yakkiho_words_when_project_has_no_product(monkeypatch):
    project = projects.create_project("render時薬機法非対象テスト", 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "s1.mp4").write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract",
            "caption": "このシミが消える美容液です",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }],
        "narration_text": "", "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_render("job_render_no_product_test", {"project_id": project["id"]})
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"  # product=Noneのプロジェクトは薬機法検査の対象外（従来どおり）
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (g) デッドコンフィグ回帰: fetch_cfg に config.json product_images.timeout_sec が
#     配線されること（従来はcollect_product_imagesへtimeout_secが一切渡らず、
#     常にpipeline/product_images.pyの既定値15秒が使われていた）
# ---------------------------------------------------------------------------

def test_fetch_cfg_wires_product_images_timeout_sec_from_config(monkeypatch):
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編", "CTA"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    captured_collect_args = {}

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        captured_collect_args["cfg"] = cfg
        return {"name": "", "url": url, "images": [], "warnings": []}

    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    # config.json未設定環境でも既定値(pipeline/config.py _DEFAULTS["product_images"]["timeout_sec"]=20)が
    # そのまま伝わることを確認する（実際にmanager.cfgを読んで検証する。決め打ちしない）。
    expected_timeout = (manager.cfg.get("product_images") or {}).get("timeout_sec")
    assert expected_timeout is not None

    project = projects.create_project("timeout_sec配線テスト", 10.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "timeout_sec配線テスト", product_url=PRODUCT_URL))
        assert captured_collect_args["cfg"]["timeout_sec"] == expected_timeout
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_fetch_cfg_falls_back_to_product_images_default_timeout_when_config_key_missing(monkeypatch):
    """config.jsonに product_images.timeout_sec が無い場合でも、
    product_images.DEFAULT_TIMEOUT_SEC にフォールバックすること（デッドコンフィグ配線の既定値）。"""
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編", "CTA"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    captured_collect_args = {}

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        captured_collect_args["cfg"] = cfg
        return {"name": "", "url": url, "images": [], "warnings": []}

    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    manager.cfg = dict(manager.cfg)
    manager.cfg["product_images"] = {}  # timeout_secキー自体を持たないconfigを模擬

    project = projects.create_project(
        "timeout_secフォールバックテスト", 10.0, "mock", status="generating", product_url=PRODUCT_URL
    )
    try:
        manager._run_generate(
            project["id"], _generate_payload(project["id"], "timeout_secフォールバックテスト", product_url=PRODUCT_URL)
        )
        assert captured_collect_args["cfg"]["timeout_sec"] == jobs_mod.product_images.DEFAULT_TIMEOUT_SEC
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (f) project["tts"] 永続化（generate/render/resumeの3経路すべて）
# ---------------------------------------------------------------------------

def test_normal_mode_persists_tts_meta_on_generate(monkeypatch):
    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編", "CTA"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project(
        tts_meta={
            "backend": "fish_audio", "duration_sec": 9.0, "is_silent": False,
            "requested_backend": "fish_audio", "fallback_reason": None,
        }
    )
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("TTSメタ永続化テスト:generate", 10.0, "mock", status="generating")
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "TTSメタ永続化テスト:generate", product_url=None))
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["tts"] == tts_meta
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_render_persists_tts_meta(monkeypatch):
    project = projects.create_project("TTSメタ永続化テスト:render", 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "s1.mp4").write_bytes(b"\x00")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "abstract", "caption": "テスト",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
            "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
        }],
        "narration_text": "テストナレーション", "bgm": None, "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_render("job_render_tts_test", {"project_id": project["id"]})
        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["tts"] == tts_meta
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# (k) 商品画像の vision 分類が jobs 経由で配線され、logo_banner が採用リストから外れて
#     product_manifest.json が product_dir に生成されること
# ---------------------------------------------------------------------------

def test_generate_classifies_product_images_and_writes_manifest_and_excludes_logo(monkeypatch):
    import json as _json

    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編1", "本編2", "CTA"])

    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        os.makedirs(dest_dir, exist_ok=True)
        return {
            "name": "テスト商品B",
            "url": url,
            "images": [
                {"path": os.path.join(dest_dir, "001.jpg"), "source": "lp", "width": 800, "height": 800},
                {"path": os.path.join(dest_dir, "002.jpg"), "source": "lp", "width": 800, "height": 800},
                {"path": os.path.join(dest_dir, "003.jpg"), "source": "lp", "width": 800, "height": 800},
            ],
            "warnings": [],
        }
    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    # 分類はモック: 001=採用、002=logo_banner(不採用)、003=採用
    def _fake_classify(paths, vision_call=jobs_mod.product_images._VISION_DEFAULT, timeout_sec=600):
        results = []
        for i, p in enumerate(paths):
            if i == 1:
                results.append({"path": p, "category": "logo_banner", "sharpness": "high",
                                 "dominant_colors": [], "adopted": False, "reason": "logo_banner"})
            else:
                results.append({"path": p, "category": "product_solo", "sharpness": "high",
                                 "dominant_colors": ["#123456"], "adopted": True, "reason": None})
        return results
    monkeypatch.setattr(jobs_mod.product_images, "classify_product_images", _fake_classify)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("分類配線テスト", 12.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        manager._run_generate(project["id"], _generate_payload(project["id"], "分類配線テスト", product_url=PRODUCT_URL))

        pdir = projects.product_dir(project["id"])
        manifest = pdir / "product_manifest.json"
        assert manifest.exists(), "product_manifest.json が生成されていない"
        payload = _json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert [e["adopted"] for e in payload["entries"]] == [True, False, True]

        saved = projects.get_project(project["id"])
        # media_images に adopted / category が入っていること
        cats = [img["category"] for img in saved["product"]["images"]]
        assert cats == ["product_solo", "logo_banner", "product_solo"]
        assert saved["product"]["images"][1]["adopted"] is False

        # backend に渡された shot に、logo画像(002.jpg)が image_path として登場していないこと
        for s in backend.shots:
            ip = s.get("image_path")
            if ip:
                assert not ip.endswith("002.jpg"), "logo_banner画像がbackendまで届いてはいけない"
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_generate_wires_reference_spec_to_assign_images_to_shots(monkeypatch):
    """reference_spec 経路（analyze_reference が spec を返す）で、
    assign_images_to_shots が reference_spec 付きで呼ばれること。"""
    captured = {}

    def _fake_run_director(theme, cfg=None, target_duration_sec=None, no_llm=False, **kwargs):
        return _plan_with_shots(["フック", "本編1", "本編2", "CTA"])
    monkeypatch.setattr(jobs_mod.director, "run_director", _fake_run_director)

    def _fake_collect(url, dest_dir, cfg, local_dir=None, fetcher=None, prober=None):
        os.makedirs(dest_dir, exist_ok=True)
        return {"name": "商品", "url": url, "images": [
            {"path": os.path.join(dest_dir, "001.jpg"), "source": "lp", "width": 800, "height": 800},
        ], "warnings": []}
    monkeypatch.setattr(jobs_mod.product_images, "collect_product_images", _fake_collect)

    monkeypatch.setattr(jobs_mod.product_images, "classify_product_images",
                        lambda paths, **kw: [{"path": p, "category": "product_solo",
                                                "sharpness": "high", "dominant_colors": [],
                                                "adopted": True, "reason": None} for p in paths])

    original_assign = jobs_mod.product_images.assign_images_to_shots
    def _wrap_assign(shots, image_paths, reference_spec=None):
        captured["reference_spec"] = reference_spec
        captured["shots_len"] = len(shots)
        return original_assign(shots, image_paths, reference_spec=reference_spec)
    monkeypatch.setattr(jobs_mod.product_images, "assign_images_to_shots", _wrap_assign)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    project = projects.create_project("reference配線テスト", 12.0, "mock", status="generating", product_url=PRODUCT_URL)
    try:
        # reference_url を渡さずに generate（reference_spec=None が渡ることを確認）
        manager._run_generate(project["id"], _generate_payload(project["id"], "reference配線テスト", product_url=PRODUCT_URL))
        assert "reference_spec" in captured
        assert captured["reference_spec"] is None  # reference_url未指定なので None
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)


def test_run_resume_persists_tts_meta_and_maps_image_path_to_backend_shot(monkeypatch):
    """(f) resume経路のtts永続化 と、resumeのbackend_shotマッピングへのimage_path追加を同時に検証する。"""
    project = projects.create_project("resume時TTS+画像パス引継ぎテスト", 10.0, "mock", status="failed")
    project["plan"] = {
        "shots": [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "p1", "caption": "c1",
            "clip_path": None, "source_duration": 5.0, "trim": {"start": 0.0, "end": 5.0},
            "image_path": "/tmp/fake_product_hook.jpg",
        }],
        "narration_text": "テスト", "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    backend = _RecordingBackend()
    monkeypatch.setattr(jobs_mod, "get_backend", lambda name, cfg: backend)
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})
    fake_render, tts_meta = _stub_render_project()
    monkeypatch.setattr(jobs_mod, "_render_project", fake_render)

    manager = _make_job_manager_without_worker(monkeypatch)
    try:
        manager._run_resume("job_resume_test", {"project_id": project["id"]})
        assert backend.shots[0]["image_path"] == "/tmp/fake_product_hook.jpg"

        saved = projects.get_project(project["id"])
        assert saved["status"] == "ready"
        assert saved["tts"] == tts_meta
    finally:
        shutil.rmtree(projects.project_dir(project["id"]), ignore_errors=True)
