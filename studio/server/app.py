# -*- coding: utf-8 -*-
"""Reel Studio: FastAPI サーバ本体。docs/STUDIO-DESIGN.md のAPI契約に厳密準拠する。

起動: `studio/start_server.sh`（127.0.0.1:8787固定）。
CORSはlocalhost/127.0.0.1のみ許可（studio/web/ のローカル開発サーバ想定）。

実装メモ（設計上の解釈・前提）:
  - PUT /api/projects/{id}/plan のリクエストボディは project.json の "plan" オブジェクト
    そのもの（{"shots":[...],...}）とする（エンドポイントが既に".../plan"を指すため）。
  - POST /api/projects はプロジェクトIDをそのままjob_idとして使う
    （返り値{"id"}を使って直後に GET /api/jobs/{id}/events を開けるようにするため）。
    POST /api/projects/{id}/render は毎回新しいjob_idを発行する。
  - エラーは常に `{"error":{"code","message"}}` + 4xx/5xx。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pipeline import render
from pipeline.config import load_config, project_root, output_dir
from studio.server import projects
from studio.server.jobs import job_manager, RenderConflictError

app = FastAPI(title="Reel Studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error_response(status_code, code, message):
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return _error_response(exc.status_code, detail["code"], detail["message"])
    return _error_response(exc.status_code, "http_error", str(detail))


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    return _error_response(422, "validation_error", str(exc.errors()))


def _not_found(message):
    raise HTTPException(status_code=404, detail={"code": "not_found", "message": message})


def _bad_request(code, message):
    raise HTTPException(status_code=400, detail={"code": code, "message": message})


def _conflict(message):
    raise HTTPException(status_code=409, detail={"code": "conflict", "message": message})


# ---------------------------------------------------------------------------
# ディレクトリ準備（StaticFilesはマウント時にディレクトリ実在が必要）
# ---------------------------------------------------------------------------

projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
output_dir()


# ---------------------------------------------------------------------------
# プロジェクト
# ---------------------------------------------------------------------------

@app.post("/api/projects", status_code=202)
async def create_project(request: Request):
    body = await request.json()
    theme = (body or {}).get("theme")
    if not theme or not isinstance(theme, str) or not theme.strip():
        _bad_request("invalid_theme", "theme は非空文字列である必要があります")

    cfg = load_config()
    duration = (body or {}).get("duration")
    target_duration_sec = float(duration) if duration else cfg.get("target_duration_sec", 30)
    backend_name = (body or {}).get("backend") or cfg.get("backend", "mock")
    if backend_name not in ("mock", "higgsfield", "cloudapi"):
        _bad_request("invalid_backend", "backend は mock/higgsfield/cloudapi のいずれかである必要があります")
    style = (body or {}).get("style") or cfg.get("default_subtitle_style", "default")
    if style not in projects.VALID_SUBTITLE_PRESETS:
        _bad_request("invalid_style", "style は {} のいずれかである必要があります".format(projects.VALID_SUBTITLE_PRESETS))

    project = projects.create_project(theme.strip(), target_duration_sec, backend_name, status="generating", style=style)
    job_manager.start_generate(project["id"], theme.strip(), target_duration_sec, backend_name, style=style)
    return {"id": project["id"]}


def _probe_duration(ffprobe_bin, path):
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return float(proc.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return 0.0


@app.post("/api/projects/import", status_code=201)
async def import_project(file: UploadFile = File(...)):
    cfg = load_config()
    suffix = Path(file.filename or "clip.mp4").suffix or ".mp4"
    if suffix.lower() not in (".mp4", ".mov"):
        _bad_request("invalid_file_type", "mp4/movのみ受け付けます")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        content = await file.read()
        tmp.write(content)

    try:
        duration = _probe_duration(cfg["ffprobe_bin"], str(tmp_path))
        if duration <= 0:
            _bad_request("invalid_video", "動画の尺を取得できませんでした（破損しているか非対応形式です）")

        project = projects.create_project(
            theme=Path(file.filename or "import").stem, target_duration_sec=duration,
            backend_name="import", status="draft",
        )
        clips_dir = projects.clips_dir(project["id"])
        clips_dir.mkdir(parents=True, exist_ok=True)
        norm_path = clips_dir / "s1.mp4"
        cmd = render.build_normalize_clip_cmd(cfg["ffmpeg_bin"], str(tmp_path), str(norm_path))
        res = render.run_ffmpeg(cmd, timeout_sec=1800)
        if res["returncode"] != 0:
            projects.delete_project_dir_if_failed_before_save(project["id"])
            _bad_request("ingest_failed", "取込の正規化に失敗しました: {}".format(res["stderr"][-500:]))

        norm_duration = _probe_duration(cfg["ffprobe_bin"], str(norm_path))
        project["plan"]["shots"] = [{
            "id": "s1", "order": 0, "enabled": True, "prompt": "", "caption": "",
            "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"), "source_duration": norm_duration,
            "trim": {"start": 0.0, "end": norm_duration},
        }]
        projects.save_project(project)
        return {"id": project["id"]}
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


@app.get("/api/projects")
async def list_projects():
    return projects.list_projects()


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")
    project = projects.get_project(project_id)
    if project is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    return project


@app.put("/api/projects/{project_id}/plan")
async def update_plan(project_id: str, request: Request):
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")
    project = projects.get_project(project_id)
    if project is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))

    plan = await request.json()
    cfg = load_config()
    from pipeline import compliance
    ng_words = list(compliance.DEFAULT_NG_WORDS)
    for w in (cfg.get("brand_rules") or {}).get("ng_words") or []:
        if w not in ng_words:
            ng_words.append(w)

    ok, errors, normalized = projects.validate_plan(project_id, plan, ng_words=ng_words)
    if not ok:
        return _error_response(400, "invalid_plan", "; ".join(errors))

    project["plan"] = normalized
    projects.save_project(project)
    return project


@app.post("/api/projects/{project_id}/render", status_code=202)
async def render_project(project_id: str):
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")

    try:
        job_id = job_manager.try_start_render(project_id)
    except RenderConflictError:
        _conflict("このプロジェクトは処理中です。完了後に再度お試しください")
        return
    if job_id is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# ジョブ進捗（SSE）
# ---------------------------------------------------------------------------

def _format_sse(payload):
    return "data: {}\n\n".format(json.dumps(payload, ensure_ascii=False))


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    from queue import Empty
    import time as time_mod

    def gen():
        if not job_manager.job_exists(job_id):
            yield _format_sse({"error": {"code": "not_found", "message": "ジョブが見つかりません: {}".format(job_id)}})
            return
        q = job_manager.subscribe(job_id)
        try:
            snapshot = job_manager.get_snapshot(job_id) or {}
            init_payload = {k: v for k, v in snapshot.items() if k != "job_id"}
            if not init_payload:
                init_payload = {"stage": "queued", "progress": 0, "message": "待機中…"}
            yield _format_sse(init_payload)
            if snapshot.get("done"):
                return

            last_heartbeat = time_mod.time()
            while True:
                try:
                    event = q.get(timeout=1.0)
                    yield _format_sse(event)
                    if event.get("done"):
                        break
                except Empty:
                    now = time_mod.time()
                    if now - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
        finally:
            job_manager.unsubscribe(job_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# アセット一覧
# ---------------------------------------------------------------------------

@app.get("/api/assets/bgm")
async def list_bgm_assets():
    manifest_path = project_root() / "assets" / "bgm" / "manifest.json"
    if not manifest_path.exists():
        return []
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/api/assets/sfx")
async def list_sfx_assets():
    manifest_path = project_root() / "assets" / "sfx" / "manifest.json"
    if not manifest_path.exists():
        return []
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 静的配信（クリップ/レンダー/アセット・Range対応はStarletteのStaticFiles標準機能）
# ---------------------------------------------------------------------------

# studio/web/api.js の mediaUrl(kind, file) 契約に合わせたマウント:
#   - kind="raw"（clip_path/renders[].path）は project.json に project_root()相対の正規パス
#     （例: "projects/<id>/clips/s1.mp4"）で保存するため、"/media/" + file が /media/projects/... に
#     一致する（projects.media_relpath_for_clip/render 参照）。
#   - kind="bgm"/"sfx"（アセットブラウザの試聴）は "/media/bgm/<file>" "/media/sfx/<file>" を直接叩くため、
#     assets/bgm・assets/sfx を個別にマウントする（/media/assets 配下ではなく直下）。
app.mount("/media/projects", StaticFiles(directory=str(projects.PROJECTS_ROOT)), name="media_projects")
app.mount("/media/output", StaticFiles(directory=str(output_dir())), name="media_output")
app.mount("/media/bgm", StaticFiles(directory=str(project_root() / "assets" / "bgm")), name="media_bgm")
app.mount("/media/sfx", StaticFiles(directory=str(project_root() / "assets" / "sfx")), name="media_sfx")
app.mount("/media/assets", StaticFiles(directory=str(project_root() / "assets")), name="media_assets")

# ---------------------------------------------------------------------------
# SPA配信（studio/web/ を同一オリジンで配信。BUG-6）
# ---------------------------------------------------------------------------
# 上記の /api/* ルートおよび /media/* マウントは、いずれもこのモジュール内で
# このマウントより「前」に登録されているため、Starletteのルーティング（登録順に
# 最初にマッチしたものを採用）により優先して処理される。このマウントは
# それらに一致しない残りすべてのパス（"/", "/app.js", "/styles/tokens.css" 等）
# を studio/web/ 配下の静的ファイルとして返す最後の受け皿として機能する。
_STUDIO_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=str(_STUDIO_WEB_DIR), html=True), name="web_spa")
