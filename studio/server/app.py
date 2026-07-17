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
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pipeline import render
from pipeline.config import load_config, project_root, output_dir
from studio.server import projects
from studio.server.jobs import job_manager, RenderConflictError, ResumeNotAllowedError, UnrenderedShotsError

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

    # product_url（任意）: 商品アフィリエイト動画モードの入口。指定時のみ http/https の
    # 有効な絶対URLであることを検証する（SSRF対策の私有IP判定等はpipeline.product_images側で
    # 別途行うため、ここではスキームとホストの形式だけを見る軽量チェックに留める）。
    product_url = (body or {}).get("product_url")
    if product_url is not None:
        if not isinstance(product_url, str) or not product_url.strip():
            _bad_request("invalid_product_url", "product_url は非空文字列である必要があります")
        product_url = product_url.strip()
        parsed = urlparse(product_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            _bad_request("invalid_product_url", "product_url は http/https の有効なURLである必要があります")

    project = projects.create_project(
        theme.strip(), target_duration_sec, backend_name, status="generating", style=style, product_url=product_url
    )
    job_manager.start_generate(
        project["id"], theme.strip(), target_duration_sec, backend_name, style=style, product_url=product_url
    )
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
    # unrendered_shot_ids はディスクに保存しない計算値（＝「続きから生成」ボタンの表示可否を
    # フロントが判定するための一覧）。project.jsonのフィールドはそのまま(既存フィールドは壊さない)。
    response = dict(project)
    response["unrendered_shot_ids"] = projects.unrendered_enabled_shot_ids(project.get("plan") or {})
    return response


@app.put("/api/projects/{project_id}/plan")
async def update_plan(project_id: str, request: Request):
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")
    project = projects.get_project(project_id)
    if project is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    if project.get("status") in ("generating", "rendering"):
        # 実行中プロジェクトへのplan書き換えを禁止する: try_start_render()の受理判定
        # （unrendered_enabled_shot_ids）〜ワーカー実行の間にPUT /planで有効ショットの
        # clip_pathをnullへ書き換えられるレースを塞ぐ（jobs.py _run_render 側の
        # 再チェックと合わせた二重の防御）。
        _conflict("このプロジェクトは処理中のため編集できません。完了後に再度お試しください")

    plan = await request.json()
    cfg = load_config()
    from pipeline import compliance
    ng_words = list(compliance.DEFAULT_NG_WORDS)
    for w in (cfg.get("brand_rules") or {}).get("ng_words") or []:
        if w not in ng_words:
            ng_words.append(w)
    ng_patterns = None
    if project.get("product"):
        # 商品アフィリエイト動画モードのプロジェクト（jobs.py _run_render/_run_generate と同じ判定条件）
        # では、PUT /plan の下書き保存経由でも薬機法NGワード/パターンを効かせる。ここで合成を
        # 省略すると、通常モードのバリデーションのみが適用され「シミが消える」等の薬機法NG表現が
        # レンダー前の編集画面から検査すり抜けでそのまま保存できてしまう（jobs.py側の再検査は
        # render開始時にしか効かないため、下書き保存の時点では素通りする）。
        for w in compliance.BEAUTY_YAKKIHO_NG_WORDS:
            if w not in ng_words:
                ng_words.append(w)
        ng_patterns = compliance.BEAUTY_YAKKIHO_NG_PATTERNS

    ok, errors, normalized = projects.validate_plan(project_id, plan, ng_words=ng_words, ng_patterns=ng_patterns)
    if not ok:
        return _error_response(400, "invalid_plan", "; ".join(errors))

    # 上のstatusチェックはリクエスト受信時点のスナップショットに基づく早期リジェクト
    # （UXとして速く弾くため）。validate_plan実行中（ディスクI/O・コンプライアンス検査で
    # 時間がかかりうる）にPOST /renderが割り込んでstatusを変えても、ここで保存直前に
    # job_manager側が再読込+アトミック保存を行うため、古いstatusで上書きされない
    # （TOCTOU対策。try_start_render/try_start_resumeと同じ_project_status_lockを共有）。
    try:
        updated_project = job_manager.try_update_plan(project_id, normalized)
    except RenderConflictError:
        _conflict("このプロジェクトは処理中のため編集できません。完了後に再度お試しください")
        return
    if updated_project is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    return updated_project


@app.post("/api/projects/{project_id}/render", status_code=202)
async def render_project(project_id: str):
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")

    try:
        job_id = job_manager.try_start_render(project_id)
    except RenderConflictError:
        _conflict("このプロジェクトは処理中です。完了後に再度お試しください")
        return
    except UnrenderedShotsError as exc:
        return _error_response(
            400, "unrendered_shots",
            "クリップが未生成の有効なショットがあります。先に生成をやり直すか無効化してください: {}".format(
                ", ".join(exc.shot_ids)
            ),
        )
    if job_id is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    return {"job_id": job_id}


@app.post("/api/projects/{project_id}/resume", status_code=202)
async def resume_project(project_id: str):
    """続きから生成: enabledかつclip_path未生成のショットだけを作り直し、揃ったら書き出しまで進める。

    status が generating/rendering の場合は409（try_start_render と同じ排他ロジック）。
    存在しなければ404。
    """
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")

    try:
        job_id = job_manager.try_start_resume(project_id)
    except RenderConflictError:
        _conflict("このプロジェクトは処理中です。完了後に再度お試しください")
        return
    except ResumeNotAllowedError:
        _conflict("このプロジェクトは失敗状態ではないため、続きから生成は不要です")
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
# トップページの出し分け（かんたんモード既定 / ?pro=1 で従来のプロUI）
# ---------------------------------------------------------------------------
# 既定 "/" は studio/web/index.html（かんたんモード）。"?pro=1" のときだけ
# studio/web/pro.html（既存の3ペインUI・変更なし）を返す。この明示ルートは
# 下の StaticFiles マウントより「前」に登録されるため、Starletteのルーティング
# （登録順に最初にマッチしたものを採用）により優先して処理される。
_STUDIO_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.get("/", include_in_schema=False)
async def index_router(request: Request):
    if request.query_params.get("pro") == "1":
        return FileResponse(str(_STUDIO_WEB_DIR / "pro.html"))
    return FileResponse(str(_STUDIO_WEB_DIR / "index.html"))


# ---------------------------------------------------------------------------
# SPA配信（studio/web/ を同一オリジンで配信。BUG-6）
# ---------------------------------------------------------------------------
# 上記の /api/* ルート・/media/* マウント・"/" の明示ルートは、いずれも
# このモジュール内でこのマウントより「前」に登録されているため優先して処理
# される。このマウントはそれらに一致しない残りすべてのパス（"/app.js",
# "/styles/tokens.css", "/simple/app.js" 等）を studio/web/ 配下の静的ファイル
# として返す最後の受け皿として機能する。
app.mount("/", StaticFiles(directory=str(_STUDIO_WEB_DIR), html=True), name="web_spa")
