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
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pipeline import render
from pipeline import plan_tier as plan_tier_mod
from pipeline import voice_catalog
from pipeline.config import load_config, project_root, output_dir
from studio.server import projects
from studio.server.jobs import (
    job_manager, RenderConflictError, ResumeNotAllowedError, UnrenderedShotsError,
    PremiereExportNotAllowedError,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ★BUG-21修正: スタック回復（status=generating/rendering のプロジェクトをfailedへ倒す処理）は
    # 実サーバプロセスの起動時のみ走らせる。以前は studio.server.jobs の import副作用
    # （JobManager.__init__内で無条件に実行）だったため、pytest収集などjobsモジュールを
    # importしただけの別プロセスが本物のprojects/を走査し、稼働中ジョブを誤ってfailedへ
    # 倒す実害があった。ここでFastAPIのlifespan(=実サーバ起動時にのみ発火)から明示的に呼ぶ。
    job_manager.recover_stuck_projects()
    yield


app = FastAPI(title="Reel Studio API", lifespan=_lifespan)

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

    # plan_tier（"free" | "paid"）: UI の2択に対応する1変数。指定時は backend 個別指定より優先し、
    # free→mock / paid→higgsfield を強制する（backend/voice/asr の混在指定を UI から撤去した設計）。
    # 未指定のとき（上級者の backend 個別指定や後方互換クライアント）は従来どおり backend を尊重する。
    raw_plan_tier = (body or {}).get("plan_tier")
    plan_tier = plan_tier_mod.normalize_tier(raw_plan_tier)
    # 「未指定」と「不正な値（typo 等）」を区別する: 非空なのに free/paid でない値は 400 で弾く。
    # （黙って backend フォールバックさせると、既定backendがhiggsfieldの環境で "fre" のtypoが
    # 有料生成を無料0コイン見積のまま開始してしまう。codex-review P1 指摘。）
    if raw_plan_tier not in (None, "") and plan_tier is None:
        _bad_request("invalid_plan_tier", "plan_tier は free/paid のいずれかである必要があります")
    requested_backend = (body or {}).get("backend")
    if plan_tier is not None:
        backend_name = plan_tier_mod.resolve_backend(plan_tier, requested_backend)
    else:
        backend_name = requested_backend or cfg.get("backend", "mock")
    if backend_name not in ("mock", "higgsfield", "cloudapi"):
        _bad_request("invalid_backend", "backend は mock/higgsfield/cloudapi のいずれかである必要があります")
    # 開始前の費用見積を billing.coins_estimated として記録する（表示・見積精度改善用）。
    # plan_tier 未指定の後方互換リクエスト（backend のみ指定）でも、実際に使う backend から
    # コースを推定して見積る（mock→free / higgsfield・cloudapi→paid）。backend=higgsfield なのに
    # 無料0コインと記録される取り違えを防ぐ（codex-review P1 指摘）。
    _billing_tier = plan_tier_mod.infer_tier(plan_tier, backend_name)
    _est = plan_tier_mod.estimate_coins(cfg, _billing_tier, duration_sec=target_duration_sec)
    billing = {
        "plan_tier": _est["plan_tier"],
        "coins_estimated": _est["coins"],
        "coins_actual": None,
        "estimate_approximate": _est["approximate"],
        "estimate_note": _est["note"],
    }
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

    # reference_url（必須）: TTP v2 移行後、Studio 経路の LLM 企画生成には参考動画URLが必須。
    # 未指定 or 空文字なら 400 応答（UI 側で「参考動画URLが必要です」の案内を表示する）。
    # 実際の解析（pipeline.reference_v2.analyze_reference_v2）はジョブ側（jobs.py _run_generate）
    # で行い、失敗した場合も job を fail させる（旧 v1 時代の fail-open は撤去）。
    reference_url = (body or {}).get("reference_url")
    if reference_url is None or not isinstance(reference_url, str) or not reference_url.strip():
        _bad_request("reference_url_required", "参考動画URLが必要です")
    reference_url = reference_url.strip()
    parsed = urlparse(reference_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        _bad_request("invalid_reference_url", "reference_url は http/https の有効なURLである必要があります")

    # voice（任意）: 声の指定。"auto"（既定=参考の話者に近い声を自動選択）またはカタログ key
    # （例 "say_kyoko" / "fish_xxxxxxxx"）。UI は次パスで露出するため、未指定は "auto" 扱い。
    voice = (body or {}).get("voice")
    if voice is not None and (not isinstance(voice, str) or not voice.strip()):
        _bad_request("invalid_voice", "voice は非空文字列（\"auto\" またはカタログ key）である必要があります")
    voice = voice.strip() if isinstance(voice, str) else None

    # bgm_mode（"auto" | "none"）: none = BGM を一切付けない（後付け派向け）。
    # 未指定は cfg.audio.bgm_mode（config.json の既定）にフォールバック。
    # サーバ側で正規化しておくことで、以降の generate ジョブ + _render_project 権威スイッチが
    # project.bgm_mode を単一の真実として参照できる。
    raw_bgm_mode = (body or {}).get("bgm_mode")
    if raw_bgm_mode is None or raw_bgm_mode == "":
        bgm_mode = (cfg.get("audio") or {}).get("bgm_mode") or "auto"
    elif raw_bgm_mode in ("auto", "none"):
        bgm_mode = raw_bgm_mode
    else:
        _bad_request("invalid_bgm_mode", "bgm_mode は auto/none のいずれかである必要があります")

    project = projects.create_project(
        theme.strip(), target_duration_sec, backend_name, status="generating", style=style, product_url=product_url,
        reference_url=reference_url, plan_tier=plan_tier, billing=billing, voice=voice, bgm_mode=bgm_mode,
    )
    job_manager.start_generate(
        project["id"], theme.strip(), target_duration_sec, backend_name, style=style, product_url=product_url,
        reference_url=reference_url, plan_tier=plan_tier, bgm_mode=bgm_mode,
    )
    return {"id": project["id"]}


@app.post("/api/estimate")
async def estimate_cost(request: Request):
    """開始前の費用見積を返す（実生成はしない・クレジットは一切消費しない）。

    Body: {"plan_tier": "free"|"paid", "duration": <sec, 任意>, "reference_url": <任意>}
    Returns: {"plan_tier","coins","shot_count","per_shot","approximate","note"}
    free は常に coins=0（0円保証）。paid は config の higgsfield.max_credits_per_shot を
    単価にした概算（approximate=True）。本 API は Higgsfield/Fish Audio を呼ばない。
    """
    body = await request.json()
    cfg = load_config()
    plan_tier = plan_tier_mod.normalize_tier((body or {}).get("plan_tier"))
    # 見積は「約◯コイン」を権威ある数字として表示するため、契約どおり free/paid のみ受け付ける。
    # 未指定・typo を黙って無料0コインに解釈すると誤表示のまま有料生成へ進みうる（codex-review P2）。
    if plan_tier is None:
        _bad_request("invalid_plan_tier", "plan_tier は free/paid のいずれかである必要があります")
    duration = (body or {}).get("duration")
    try:
        duration_sec = float(duration) if duration else cfg.get("target_duration_sec", 30)
    except (TypeError, ValueError):
        duration_sec = cfg.get("target_duration_sec", 30)
    return plan_tier_mod.estimate_coins(cfg, plan_tier, duration_sec=duration_sec)


@app.get("/api/voices")
async def list_voices(tier: str = None):
    """選べる声のカタログを返す（読み取り専用・クレジット消費なし）。

    Query: tier="free"|"paid"（任意）。指定時はその tier の声だけへ絞り込む。
    Returns: {"voices": [{"key","tier","label","gender","engine","engine_voice_id","pitch"}, ...]}
    free = macOS `say` の日本語ボイス（課金ゼロ）、paid = config.voices.fish の登録声。
    UI の「声をえらぶ」はこの一覧＋先頭の「おまかせ(auto)」で構成する。
    """
    cfg = load_config()
    catalog = voice_catalog.build_catalog(cfg)
    if tier in ("free", "paid"):
        catalog = [e for e in catalog if e.get("tier") == tier]
    return {"voices": catalog}


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


@app.post("/api/projects/{project_id}/premiere-export", status_code=202)
async def premiere_export(project_id: str):
    """「Premiereで編集」ボタン: readyなプロジェクトから書き出しパッケージ生成ジョブを起動する。

    status != "ready" のプロジェクト（未完成・処理中・失敗中）は409で拒否する
    （project.status自体は変更しない読み取り専用の副産物生成のため、renderのような
    generating/rendering遷移は行わない）。
    """
    if not projects.is_safe_project_id(project_id):
        _bad_request("invalid_project_id", "project_idの形式が不正です")

    try:
        job_id = job_manager.try_start_premiere_export(project_id)
    except PremiereExportNotAllowedError:
        _conflict("このプロジェクトは完成していないため、Premiereへの書き出しはできません")
        return
    if job_id is None:
        _not_found("プロジェクトが見つかりません: {}".format(project_id))
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# ヘルスチェック（claude 実疎通 / ffmpeg / ffprobe / yt-dlp）
# ---------------------------------------------------------------------------

def _resolve_bin(raw, default_name):
    """config の bin 指定を絶対パスへ解決する（相対パス＋区切り有りは project_root 基準）。"""
    import os as _os
    if not raw:
        return default_name
    raw = str(raw)
    if _os.path.sep in raw and not _os.path.isabs(raw):
        return str(project_root() / raw)
    return raw


def _check_cli(bin_path, version_args, timeout_sec=30):
    """指定バイナリを --version 系で叩き、疎通可否を返す（{"ok","detail"}）。"""
    try:
        proc = subprocess.run(
            [bin_path] + list(version_args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec,
        )
    except FileNotFoundError:
        return {"ok": False, "detail": "見つかりません: {}".format(bin_path)}
    except Exception as exc:
        return {"ok": False, "detail": "起動に失敗: {}".format(str(exc)[:150])}
    if proc.returncode != 0:
        return {"ok": False, "detail": "exit {}: {}".format(proc.returncode, proc.stderr.decode("utf-8", "replace")[:150])}
    first = (proc.stdout.decode("utf-8", "replace").strip().splitlines() or [""])[0]
    return {"ok": True, "detail": first[:120]}


def compute_health(check_claude=True):
    """claude 実疎通 + ffmpeg/ffprobe/yt-dlp の3点を検査して結果 dict を返す。

    check_claude=False のときは claude 疎通をスキップ（api_error 分類のみ・課金ゼロ）。
    """
    cfg = load_config()
    checks = {}
    checks["ffmpeg"] = _check_cli(cfg.get("ffmpeg_bin") or "ffmpeg", ["-version"])
    checks["ffprobe"] = _check_cli(cfg.get("ffprobe_bin") or "ffprobe", ["-version"])
    ytdlp_bin = _resolve_bin((cfg.get("reference") or {}).get("yt_dlp_bin"), "yt-dlp")
    checks["yt_dlp"] = _check_cli(ytdlp_bin, ["--version"])
    if check_claude:
        try:
            from pipeline import claude_runner
            checks["claude"] = claude_runner.probe_claude()
        except Exception as exc:
            checks["claude"] = {"ok": False, "detail": "疎通確認に失敗: {}".format(str(exc)[:150])}
    ok = all(c.get("ok") for c in checks.values())
    return {"ok": ok, "checks": checks}


@app.get("/api/health")
def health(claude: int = 1):
    """AI(claude)/ffmpeg/ffprobe/yt-dlp の疎通を検査する。

    `?claude=0` で claude 実疎通をスキップ（軽量チェック・課金ゼロ）。NG時は ok=false。

    ★codex-review P1（2026-07-26）対策: `def`（同期）で宣言する。compute_health() は
    複数の subprocess（ffmpeg/ffprobe/yt-dlp/claude、claude は最大45秒）を同期に叩くため、
    `async def` だと Uvicorn のイベントループスレッド上で最大45秒ブロックし、他のAPI/SSE
    ストリームがフリーズする（依存が落ちているまさにその時にアプリごと固まる）。同期 def
    にすると Starlette は自動で threadpool へディスパッチする。
    """
    result = compute_health(check_claude=bool(claude))
    status = 200 if result["ok"] else 503
    return JSONResponse(status_code=status, content=result)


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
