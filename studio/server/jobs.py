# -*- coding: utf-8 -*-
"""Reel Studio: 非同期ジョブ + SSE進捗（video-auto-editor/server/jobs.py・events.py のパターンを踏襲）。

直列キュー（queue.Queue + ワーカースレッド1本）でジョブを実行し、ジョブごとの進捗を
サブスクライバキューへpublishする。SSEイベント形式は docs/STUDIO-DESIGN.md のAPI契約に
厳密に従う: 進捗イベントは `{"stage","progress","message"}`、終了イベントは
`{"done":true,"ok":bool,"path":str|None}`（フロント側の実装と契約を合わせるため、
video-auto-editorの `{"type":...}` 形式とは異なる点に注意）。

ジョブ種別:
  - "generate": theme -> director(企画) -> compliance -> visual(各ショット生成+正規化)
                -> tts -> subtitles -> render まで通し、projectを作る（POST /api/projects）。
  - "render":   既存projectのplan（Studioでの編集結果）から再レンダリングする
                （POST /api/projects/{id}/render）。trim/BGM gain/SFX/テロップスタイルを反映する。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

# ジョブ種別 "resume" を追加（続きから生成）:
#   status が failed で、有効(enabled)ショットの一部にclip_path=nullが残っているプロジェクトに対し、
#   未生成ショットだけをバックエンドで生成し直し、揃ったら_render_projectで書き出しまで進める。
#   途中でサーバが再起動した場合（generating/rendering のまま止まっていた場合）は、
#   サーバ起動時（FastAPIのstartup/lifespan経由）のスタック回復でstatus="failed"に倒し、
#   resumeで再開できるようにする（JobManager()の生成自体では回復は走らない。BUG-21）。

import contextlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

from pipeline import compliance
from pipeline import director
from pipeline import edit_profile
from pipeline import product_images
from pipeline import render
from pipeline import scenes as scenes_mod
from pipeline import subtitles
from pipeline import tts as tts_mod
from pipeline.config import load_config, project_root
from pipeline.visual import get_backend
from premiere import driver as premiere_driver
from premiere import package as premiere_package
from premiere import setup_check as premiere_setup_check
from studio.server import projects

ASSETS_DIR = project_root() / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
BGM_MANIFEST = ASSETS_DIR / "bgm" / "manifest.json"

_FFMPEG_TIMEOUT_SEC = 1800
_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

# シーングループ（クリップ再利用）関連の定数。
# 1回の生成リクエストで作るシーンマスターの合計尺の上限（超えたらショット境界で分割生成する）。
_SCENE_MAX_REQUEST_SEC = 12.0
# シーンマスターから各ショットを切り出す際の content 尺の下限（ffmpegの -t 0 秒回避の保険）。
_MIN_CUT_SEC = 0.05
_SCENE_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

TERMINAL = ("done",)  # SSE側は最終イベント {"done":true,...} の送出をもって終端とみなす

# _render_project がテロップアニメーション解決のため参照する編集プロファイル名。
# run.py の CLI 経路と揃えるため定数化する（マジック文字列の散在を避ける）。
_TELOP_PROFILE_NAME = "ttp_reference"


def _new_job_id():
    return "job_" + time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _safe_scene_key(scene_key):
    """scene_key（director由来の任意文字列）をファイル名に安全な形へ正規化する。"""
    return _SCENE_KEY_SAFE_RE.sub("_", str(scene_key))


def _probe_duration(ffprobe_bin, path):
    """ffprobeで動画の実尺(秒)を測る。取得できなければ None を返す（呼び出し側で計画尺へfallback）。"""
    try:
        proc = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        return float(proc.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return None


@contextlib.contextmanager
def _scaled_credit_limit(backend, factor):
    """シーンマスター生成中だけ backend.max_credits_per_shot を factor 倍に引き上げる。

    シーンマスターは構成ショット数ぶんの尺を1本で生成するため、単ショット用の上限のままだと
    見積コストが上限を超えて誤って中断されてしまう。ここで「構成ショット数×上限」を許容する
    （＝1シーン=1生成でクレジット消費は1本ぶんに抑えつつ、上限だけスケールさせる）。
    max_credits_per_shot 属性を持たないバックエンド（mock 等）や factor<=1 のときは何もしない。
    """
    prev = getattr(backend, "max_credits_per_shot", None)
    changed = False
    if isinstance(prev, (int, float)) and not isinstance(prev, bool) and factor and factor > 1:
        try:
            backend.max_credits_per_shot = prev * factor
            changed = True
        except Exception:
            changed = False
    try:
        yield
    finally:
        if changed:
            try:
                backend.max_credits_per_shot = prev
            except Exception:
                pass


def _parse_loudnorm_json(stderr_text):
    m = _LOUDNORM_JSON_RE.search(stderr_text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return {
            "measured_I": data["input_i"],
            "measured_TP": data["input_tp"],
            "measured_LRA": data["input_lra"],
            "measured_thresh": data["input_thresh"],
            "offset": data.get("target_offset", "0.0"),
        }
    except Exception:
        return None


def _default_analyze_reference(url, cfg, progress_cb=None):
    """pipeline.reference.analyze_reference への遅延importラッパー。

    pipeline/reference.py は別ワーカーが並行実装中のモジュールのため、jobs.py の
    import時点（モジュール読み込み時）にトップレベルで `from pipeline import reference` を
    行うと、未実装/未マージの間はjobs.py自体のimportが壊れてしまう。ここで実際に参考動画URLが
    指定され解析が必要になった時点（関数呼び出し時）にのみimportすることでそれを避ける。

    モジュール変数 `analyze_reference`（このラッパー関数）自体をテストでmonkeypatchすれば、
    pipeline.reference の実装有無やネットワークアクセスに関わらず、_run_generate側の
    分岐ロジック（成功/cached/失敗のfail-open処理）だけを単体テストできる。
    """
    from pipeline import reference as reference_mod
    return reference_mod.analyze_reference(url, cfg, progress_cb=progress_cb)


analyze_reference = _default_analyze_reference


def _plan_narration_became_stale(old_plan, new_plan):
    """PUT /plan保存前後で narration_text（全文ナレーション）または有効(enabled)ショットの
    trim(start/end) が変わったかどうかを判定する。

    project["narration_segments"]（_run_generateが shots[].narration_jp から作る、
    音声主導タイミング同期モード用の「ショットid -> 断片テキスト」）は、保存時点の
    narration_text/各ショットのtrimと対応づいている前提でしか正しく使えない。
    ユーザーがStudio/簡易UIでnarration_textを書き換えたり、いずれかの有効ショットの
    trimを変えたりした後にそのまま古いnarration_segmentsでレンダリングすると、
    古い音声がそのまま残る（テキスト変更が音声に反映されない）か、trimでずれたショット
    境界に対して古い断片尺のまま同期させてしまう。ここでTrueを返した場合、呼び出し側
    (try_update_plan)がnarration_segmentsをクリアし、次回レンダリングを全文方式(full)へ
    自動フォールバックさせる。
    """
    old_plan = old_plan or {}
    if (old_plan.get("narration_text") or "") != (new_plan.get("narration_text") or ""):
        return True
    old_trim_by_id = {
        s.get("id"): s.get("trim")
        for s in (old_plan.get("shots") or [])
        if isinstance(s, dict)
    }
    for shot in new_plan.get("shots") or []:
        if not isinstance(shot, dict) or not shot.get("enabled", True):
            continue
        if old_trim_by_id.get(shot.get("id")) != shot.get("trim"):
            return True
    return False


_DURATION_DRIFT_WARN_THRESHOLD_SEC = 3.0


def _duration_drift_info(out_duration, target_duration_sec):
    """レンダリング実出力尺と目標尺(project.target_duration_sec)の差分を計算する。

    Returns: (drift_sec: float|None, warning_message: str|None)
    target_duration_secが取得できない場合は (None, None)。
    |drift|>3.0秒の場合のみwarning_messageを返す。この警告はSSE通知/project["tts"]記録用の
    可視化に留め、QAの合否判定式（qa/qa_check.py）は変更しない（誤検知でrunを止めるリスクを
    避けるため。音声の実尺に合わせて意図的に尺が動くのは同期モードの仕様）。
    """
    if target_duration_sec is None:
        return None, None
    try:
        target = float(target_duration_sec)
    except (TypeError, ValueError):
        return None, None
    drift = out_duration - target
    warning = None
    if abs(drift) > _DURATION_DRIFT_WARN_THRESHOLD_SEC:
        direction = "長く" if drift > 0 else "短く"
        warning = "※目標尺より{:.1f}秒{}なりました（音声に合わせたため）".format(abs(drift), direction)
    return round(drift, 2), warning


def _resolve_bgm_by_mood(mood, seed=None, project_id=None):
    """ムードから BGM ファイル名を1曲返す（pipeline.bgm_library.pick_bgm 経由）。

    後方互換のため戻り値の型（str filename or None）は従来と同一。
    seed（既定=project_id）で決定論選曲、直近使用2曲は自動回避される。
    project_id を渡すと選曲結果を assets/bgm/.history.json に永続化する。
    """
    if not mood or mood == "none":
        return None
    try:
        from pipeline import bgm_library
    except Exception:
        return None
    entry = bgm_library.pick_bgm(mood, seed=(seed if seed is not None else project_id),
                                 record_project_id=project_id)
    return entry.get("file") if entry else None


class RenderConflictError(Exception):
    """既に generating/rendering 中のプロジェクトへの重複render要求。"""


class ResumeNotAllowedError(Exception):
    """failed以外のプロジェクトへのresume要求（再開の必要がない状態）。"""


class PremiereExportNotAllowedError(Exception):
    """ready以外のプロジェクトへのPremiere書き出し要求（完成前は書き出せない）。"""


class PremiereExportInProgressError(PremiereExportNotAllowedError):
    """同一project_idのPremiere書き出しが既に実行中（受理済み〜完了前）の重複要求。

    PremiereExportNotAllowedErrorのサブクラスとして定義する。studio/server/app.py の
    `except PremiereExportNotAllowedError:`（409応答）はPythonの例外捕捉がサブクラスにも
    及ぶため、app.py側を無改修のままこのエラーも409として扱える。
    """


class UnrenderedShotsError(Exception):
    """有効(enabled)ショットにclip_path未生成のものが含まれるrender要求。

    render開始前に明確なエラーとして拒否する（jobワーカー内でこっそり失敗させない）。
    """

    def __init__(self, shot_ids):
        self.shot_ids = list(shot_ids)
        super().__init__("未生成のショットがあります: {}".format(", ".join(self.shot_ids)))


class JobManager:
    def __init__(self):
        self.cfg = load_config()
        self._lock = threading.Lock()
        self._jobs = {}  # job_id -> snapshot dict（最新イベント含む簡易状態）
        self._queue = Queue()
        self._sub_lock = threading.Lock()
        self._subscribers = {}
        self._project_status_lock = threading.Lock()  # render開始時のstatus確認+書換をアトミックにする
        # 実行中のpremiere_exportジョブのproject_id集合（_project_status_lockで保護）。
        # try_start_premiere_exportで追加し、_run_premiere_exportの完了/失敗時(finally)で
        # 必ず解放する。build_package()は呼び出しごとにTTS再合成等の副作用を伴うため、
        # 同一project_idの二重投入をここで409相当のエラーとして拒否する（BUG修正:
        # premiere_export二重実行レース）。
        self._premiere_exports_in_progress = set()
        # ★BUG-21修正: 以前はここで self._recover_stuck_projects() を呼んでいたが、
        # それだと「studio.server.jobs を import しただけ」（pytest収集時のimportや
        # 他プロセスからのimport等）で本物の projects/ ディレクトリを走査し、
        # 稼働中(status=generating/rendering)のプロジェクトを勝手にfailedへ倒してしまう
        # 実害があった（サーバでresume生成中に別プロセスのpytestが走り、生成中ジョブが
        # 強制failed化された）。回復処理は実サーバプロセスの起動時のみ走らせるべきなので、
        # ここでは呼ばず、studio/server/app.py の FastAPI起動イベント(lifespan)から
        # 明示的に recover_stuck_projects() を呼ぶ設計にする。
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    # --- 起動時のスタック回復 -------------------------------------------
    # 呼び出しは実サーバプロセスの起動時のみ（studio/server/app.py の startup/lifespan）。
    # JobManager() を生成しただけでは走らない（import副作用を断つため）。

    def recover_stuck_projects(self):
        """サーバ起動時、status が generating/rendering のまま残っているプロジェクトを回復する。

        以前のプロセスがクラッシュ/再起動で終了すると、ワーカースレッド（メモリ上の状態）が
        消えるため project.json 上の status だけが generating/rendering のまま永久に残り、
        UIが「処理中」表示で固まってしまう。かつ従来はここから再開する手段がUI/APIどちらにも
        無かった。ここで明確に failed へ倒し、resume で再開できることを案内する。
        """
        try:
            summaries = projects.list_projects()
        except Exception:
            return
        for summary in summaries:
            project_id = summary.get("id")
            if not project_id:
                continue
            project = projects.get_project(project_id)
            if project is None:
                continue
            prior_status = project.get("status")
            if prior_status not in ("generating", "rendering"):
                continue
            kind = "generate" if prior_status == "generating" else "render"
            message = "サーバ再起動により処理が中断されました。続きから生成で再開できます。"
            project["status"] = "failed"
            project["error"] = message
            projects.append_error_history(project, kind, message)
            projects.save_project(project)

    # --- 公開API -----------------------------------------------------

    def start_generate(self, project_id, theme, target_duration_sec, backend_name, style="default", product_url=None,
                        reference_url=None):
        job_id = project_id  # POST /api/projects はプロジェクトIDそのものをjob_idとして使う（設計判断）
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "kind": "generate", "status": "queued"}
        self._queue.put(("generate", job_id, {
            "project_id": project_id, "theme": theme,
            "target_duration_sec": target_duration_sec, "backend_name": backend_name,
            "style": style, "product_url": product_url, "reference_url": reference_url,
        }))
        return job_id

    def start_render(self, project_id):
        job_id = _new_job_id()
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "kind": "render", "status": "queued"}
        self._queue.put(("render", job_id, {"project_id": project_id}))
        return job_id

    def try_start_render(self, project_id):
        """project.statusの確認とrendering遷移+enqueueをアトミックに行う。

        2つのrenderリクエストがほぼ同時に来ても、片方だけが受理され
        もう片方は RenderConflictError になることを保証する
        （HTTPハンドラの外側でstatusを見てから遷移する2段階だと競合が生じるため、
        1つのロックの中で「読む→判定→書く→enqueue」まで完結させる）。
        """
        with self._project_status_lock:
            project = projects.get_project(project_id)
            if project is None:
                return None
            if project.get("status") in ("generating", "rendering"):
                raise RenderConflictError(project_id)
            unrendered_ids = projects.unrendered_enabled_shot_ids(project.get("plan") or {})
            if unrendered_ids:
                raise UnrenderedShotsError(unrendered_ids)
            project["status"] = "rendering"
            projects.save_project(project)
            return self.start_render(project_id)

    def try_update_plan(self, project_id, normalized_plan):
        """PUT /api/projects/{id}/plan の保存をtry_start_render/try_start_resumeと同じ
        _project_status_lockの下でアトミックに行う。

        TOCTOU対策: update_planハンドラがリクエスト受信時に読んだprojectスナップショット
        （status="ready"等）をそのまま保存すると、validate_plan実行中（ディスクI/O・
        コンプライアンス検査で時間がかかる）にPOST /renderが割り込んでstatusを
        "rendering"へ遷移させても、古いstatusで上書きしてしまうレースがあった。
        ここで保存直前にprojectを再読込し、statusが生成/レンダリング中であれば
        RenderConflictErrorを送出して保存自体を行わない
        （= try_start_render/try_start_resumeと同じロックで直列化されるため、
        このメソッド実行中に別スレッドがstatusを書き換えることもない）。

        narration_segmentsの陳腐化ガード: 保存前後で narration_text または有効ショットの
        trim が変わっていれば project["narration_segments"] をクリアし、次回レンダリングを
        全文方式(full)へ自動フォールバックさせる（_plan_narration_became_stale参照。古い音声の
        残留防止＋ユーザーのtrim編集意図の尊重）。project["tts"]（直近レンダーのbackend/mode
        表示）自体はここでは更新しない＝次にレンダリングが実行されるまでは直近の実績値の
        ままで、これは「まだ再レンダリングしていない」という事実と矛盾しない。

        Returns: 保存後のproject dict（更新後の最新状態）。存在しなければNone。
        Raises: RenderConflictError（保存直前の再読込でstatusがgenerating/rendering中）。
        """
        with self._project_status_lock:
            project = projects.get_project(project_id)
            if project is None:
                return None
            if project.get("status") in ("generating", "rendering"):
                raise RenderConflictError(project_id)
            if _plan_narration_became_stale(project.get("plan"), normalized_plan):
                project["narration_segments"] = {}
            project["plan"] = normalized_plan
            projects.save_project(project)
            return project

    def start_resume(self, project_id):
        job_id = _new_job_id()
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "kind": "resume", "status": "queued"}
        self._queue.put(("resume", job_id, {"project_id": project_id}))
        return job_id

    def try_start_resume(self, project_id):
        """続きから生成の受理判定+status遷移+enqueueをアトミックに行う（try_start_renderと同じ設計）。

        Returns: job_id（受理）/ None（プロジェクトが存在しない）。
        Raises: RenderConflictError（既にgenerating/rendering中）/
                ResumeNotAllowedError（failed以外＝再開の必要がない状態。ready等に
                resumeが通ると不要な全体再レンダリングとrenders履歴の重複が起きるため拒否）。
        """
        with self._project_status_lock:
            project = projects.get_project(project_id)
            if project is None:
                return None
            if project.get("status") in ("generating", "rendering"):
                raise RenderConflictError(project_id)
            if project.get("status") != "failed":
                raise ResumeNotAllowedError(project_id)
            project["status"] = "generating"
            projects.save_project(project)
            return self.start_resume(project_id)

    def start_premiere_export(self, project_id):
        job_id = _new_job_id()
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "kind": "premiere_export", "status": "queued"}
        self._queue.put(("premiere_export", job_id, {"project_id": project_id}))
        return job_id

    def try_start_premiere_export(self, project_id):
        """「Premiereで編集」ボタンの受理判定+enqueueをアトミックに行う（try_start_render等と同じ設計）。

        Premiere書き出しはproject.statusを変更しない読み取り専用の副産物生成のため、
        try_start_render/try_start_resumeのようにstatusを書き換えることはしないが、
        受理判定は同じ_project_status_lockの下で行い、render/resume等の状態遷移と
        レースしないようにする。

        重複実行ガード: 同一project_idのpremiere_exportジョブが既に受理済み〜完了前であれば
        PremiereExportInProgressError（PremiereExportNotAllowedErrorのサブクラス。409相当）
        で拒否する。build_package()は毎回TTSを再合成しタイムスタンプ付きpackage_dirを作る
        非冪等な処理のため、同一プロジェクトへの二重投入は無駄な競合を招く
        （BUG修正: premiere_export二重実行レース）。

        Returns: job_id（受理）/ None（プロジェクトが存在しない）。
        Raises: PremiereExportNotAllowedError（status != "ready"。完成前は書き出せない）/
                PremiereExportInProgressError（同一project_idが既に実行中）。
        """
        with self._project_status_lock:
            project = projects.get_project(project_id)
            if project is None:
                return None
            if project.get("status") != "ready":
                raise PremiereExportNotAllowedError(project_id)
            if project_id in self._premiere_exports_in_progress:
                raise PremiereExportInProgressError(project_id)
            self._premiere_exports_in_progress.add(project_id)
            return self.start_premiere_export(project_id)

    def subscribe(self, job_id):
        q = Queue()
        with self._sub_lock:
            self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id, q):
        with self._sub_lock:
            lst = self._subscribers.get(job_id, [])
            if q in lst:
                lst.remove(q)

    def get_snapshot(self, job_id):
        with self._lock:
            return dict(self._jobs.get(job_id) or {}) or None

    def job_exists(self, job_id):
        with self._lock:
            return job_id in self._jobs

    def _publish(self, job_id, event):
        with self._lock:
            snap = self._jobs.setdefault(job_id, {"job_id": job_id})
            snap.update(event)
        with self._sub_lock:
            subs = list(self._subscribers.get(job_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def _emit(self, job_id, stage, progress, message):
        self._publish(job_id, {"stage": stage, "progress": progress, "message": message})

    def _finish(self, job_id, ok, path=None):
        self._publish(job_id, {"done": True, "ok": ok, "path": path})

    # --- ワーカー ------------------------------------------------------

    def _worker_loop(self):
        while True:
            kind, job_id, payload = self._queue.get()
            try:
                if kind == "generate":
                    self._run_generate(job_id, payload)
                elif kind == "resume":
                    self._run_resume(job_id, payload)
                elif kind == "premiere_export":
                    self._run_premiere_export(job_id, payload)
                else:
                    self._run_render(job_id, payload)
            except Exception as exc:  # 予期しない例外はジョブ失敗として握りつぶす（ワーカーは止めない）
                self._emit(job_id, "error", 100, "予期しないエラー: {}".format(exc))
                self._finish(job_id, ok=False, path=None)
            finally:
                self._queue.task_done()

    # --- generate --------------------------------------------------------

    def _run_generate(self, job_id, payload):
        project_id = payload["project_id"]
        theme = payload["theme"]
        target_duration_sec = payload["target_duration_sec"]
        backend_name = payload["backend_name"]
        style = payload.get("style") or "default"
        product_url = payload.get("product_url")
        cfg = self.cfg

        def fail(message):
            project = projects.get_project(project_id)
            if project is not None:
                project["status"] = "failed"
                project["error"] = message
                projects.append_error_history(project, "generate", message)
                projects.save_project(project)
            self._emit(job_id, "error", 100, message)
            self._finish(job_id, ok=False, path=None)

        # --- 商品画像の取得（商品アフィリエイト動画モード。product_url指定時のみ） -----------
        # director企画生成の前に行う: director側へ product={"name","url","image_count"} を渡し
        # 企画そのものを商品訴求向けに寄せるため。画像0枚/取得失敗でもfailさせず、
        # warningsを進捗メッセージで通知したうえで通常モードのまま続行する
        # （TTS同様「パイプライン全体が必ず完成すること」を優先する既存設計を踏襲）。
        product_info = None
        product_image_paths = []
        if product_url:
            self._emit(job_id, "product", 3, "商品画像を取得中…")
            pcfg = cfg.get("product_images") or {}
            local_dir_conf = pcfg.get("local_dir")
            local_dir_abs = str(project_root() / local_dir_conf) if local_dir_conf else None
            fetch_cfg = {
                "max_images": pcfg.get("max_images", product_images.DEFAULT_MAX_IMAGES),
                "min_image_short_side": pcfg.get("min_short_side", product_images.DEFAULT_MIN_SHORT_SIDE),
                "ffprobe_bin": cfg.get("ffprobe_bin"),
                # デッドコンフィグ対策: config.json product_images.timeout_sec を実際のLP/画像フェッチの
                # タイムアウトへ配線する（従来はcollect_product_images側で読まれず常に既定値15秒だった）。
                "timeout_sec": pcfg.get("timeout_sec", product_images.DEFAULT_TIMEOUT_SEC),
            }
            try:
                result = product_images.collect_product_images(
                    product_url, str(projects.product_dir(project_id)), fetch_cfg, local_dir=local_dir_abs
                )
            except Exception as exc:
                result = {
                    "name": "", "url": product_url, "images": [],
                    "warnings": ["商品画像の取得中に予期しないエラーが発生しました: {}".format(exc)],
                }
            product_image_paths = [img["path"] for img in (result.get("images") or [])]
            media_images = [
                dict(img, path=projects.media_relpath_for_product(project_id, Path(img["path"]).name))
                for img in (result.get("images") or [])
            ]
            product_info = {
                "name": result.get("name") or "",
                "url": product_url,
                "images": media_images,
                "warnings": result.get("warnings") or [],
            }
            project = projects.get_project(project_id)
            if project is not None:
                project["product"] = product_info
                projects.save_project(project)
            if result.get("warnings"):
                self._emit(job_id, "product", 4, "商品画像の取得で注意: {}".format("; ".join(result["warnings"])))
            if product_image_paths:
                self._emit(job_id, "product", 5, "商品画像を{}枚取得しました".format(len(product_image_paths)))
            else:
                self._emit(job_id, "product", 5, "商品画像を取得できなかったため通常モードで続行します")

        # --- 参考動画の解析（fail-open。参考動画リンク指定時のみ・director企画生成の直前） ------
        # 成功時はdirector側へ reference=spec を渡し、参考動画の構成・テンポを企画に反映させる。
        # 解析に失敗（analyze_referenceがok:Falseを返す/例外を送出）してもfailさせない: 警告
        # メッセージをemitしたうえで通常の台本生成のまま続行する（product_urlと同じ「パイプライン
        # 全体が必ず完成すること」を優先する既存設計を踏襲。BUG-10コメント参照）。
        reference_spec = None
        reference_url = payload.get("reference_url")
        if reference_url:
            self._emit(job_id, "reference", 4, "参考動画を解析中…")
            try:
                ref_result = analyze_reference(reference_url, cfg, progress_cb=None)
            except Exception as exc:
                ref_result = {
                    "ok": False, "spec": None, "source": None, "cached": False, "warnings": [],
                    "error": "参考動画の解析中に予期しないエラーが発生しました: {}".format(exc),
                }
            if ref_result.get("ok"):
                if ref_result.get("cached"):
                    self._emit(job_id, "reference", 5, "解析結果を再利用します")
                reference_spec = ref_result.get("spec")
                spec_duration = (reference_spec or {}).get("duration_sec")
                if spec_duration:
                    # spec["duration_sec"] を 15〜60秒にclampしてtarget_duration_secを上書きする
                    # （参考動画の尺を優先しつつ、あまりに極端な値でパイプライン全体が壊れないようにする）。
                    target_duration_sec = max(15.0, min(60.0, float(spec_duration)))
                    # project.json側の表示値も上書き後の実効値に揃える（directorへは18秒で渡るのに
                    # projectのtarget_duration_secが作成時の値のまま、という不整合の防止。実機E2Eで発見）。
                    _proj = projects.get_project(project_id)
                    if _proj is not None:
                        _proj["target_duration_sec"] = target_duration_sec
                        projects.save_project(_proj)
                reference_meta = {
                    "url": reference_url,
                    "ok": True,
                    "source": ref_result.get("source"),
                    "cached": bool(ref_result.get("cached")),
                    "duration_sec": (reference_spec or {}).get("duration_sec"),
                    "beats_count": len((reference_spec or {}).get("beats") or []),
                    "warnings": ref_result.get("warnings") or [],
                }
            else:
                reason = ref_result.get("error") or "不明なエラー"
                self._emit(
                    job_id, "reference", 5,
                    "参考動画を解析できなかったため通常の台本生成で続行します: {}".format(reason),
                )
                reference_meta = {"url": reference_url, "ok": False, "error": reason}
                reference_spec = None
            project = projects.get_project(project_id)
            if project is not None:
                project["reference"] = reference_meta
                projects.save_project(project)

        self._emit(job_id, "director", 5, "企画を生成中…")
        director_product = None
        if product_url:
            director_product = {
                "name": (product_info or {}).get("name") or "",
                "url": product_url,
                "image_count": len(product_image_paths),
            }
        try:
            plan = director.run_director(
                theme, cfg, target_duration_sec=target_duration_sec, no_llm=False, style=style,
                product=director_product, reference=reference_spec,
            )
        except Exception as exc:
            fail("企画生成に失敗しました: {}".format(exc))
            return
        self._emit(job_id, "director", 20, "企画生成が完了しました（source={}）".format(plan.get("meta", {}).get("source")))

        # 台本メタ（どのモデルで作られたか・AI生成かルールベース代替か）をprojectに永続化する。
        # UI（かんたんモード）はこれを見て「台本AI: <model_used>」または
        # 「台本: 自動テンプレ(AI接続失敗)」の注意表示を出す。
        plan_meta = plan.get("meta") or {}
        director_meta = {
            "model_used": plan_meta.get("model_used"),
            "source": plan_meta.get("source"),
            "quality": plan_meta.get("quality"),
        }
        project = projects.get_project(project_id)
        if project is not None:
            project["director"] = director_meta
            projects.save_project(project)

        ng_words = list(compliance.DEFAULT_NG_WORDS)
        for w in (cfg.get("brand_rules") or {}).get("ng_words") or []:
            if w not in ng_words:
                ng_words.append(w)
        ng_patterns = None
        if product_url:
            # 商品アフィリエイト動画モードのときのみ薬機法NGワード/パターンを合成する
            # （通常モードの挙動は不変に保つ。ng_words/ng_patternsは呼び出し側が明示的に
            # 合成して渡す設計＝pipeline/compliance.pyのコメント方針に従う）。
            for w in compliance.BEAUTY_YAKKIHO_NG_WORDS:
                if w not in ng_words:
                    ng_words.append(w)
            ng_patterns = compliance.BEAUTY_YAKKIHO_NG_PATTERNS
        check = compliance.check_plan(plan, ng_words=ng_words, ng_patterns=ng_patterns)
        if not check["ok"]:
            fail("コンプライアンス違反のため生成を停止しました: {}".format(check["violations"]))
            return

        shots = plan.get("shots", [])
        if product_url and product_image_paths:
            shots = product_images.assign_images_to_shots(shots, product_image_paths)
        pdir = projects.project_dir(project_id)
        clips_dir = projects.clips_dir(project_id)
        clips_dir.mkdir(parents=True, exist_ok=True)

        # ★BUG-10修正: director企画（shotsの内容）は、以降のビジュアル生成が途中で失敗しても
        # project.json から失われてはいけない。ここで先にclip_path=Noneの状態でplanを保存し、
        # ショットが1本も生成できずに失敗しても plan.shots に企画内容（prompt/caption/尺）が
        # 残るようにする。以前は生成ループを抜けた後にまとめて project["plan"] を書いていた
        # ため、途中失敗時は create_project() 時点の空のplan（shots:[]）のままになっていた。
        # 後方互換: plan に既に bgm.file が明示指定済みならそれを尊重（Studioで手動指定した場合等）
        _explicit_bgm = plan.get("bgm") if isinstance(plan.get("bgm"), dict) else None
        if _explicit_bgm and _explicit_bgm.get("file"):
            bgm_file = _explicit_bgm.get("file")
        else:
            bgm_file = _resolve_bgm_by_mood(plan.get("bgm_mood"), project_id=project_id)

        def _planned_shot(i, shot):
            base = {
                "id": shot["id"],
                "order": i,
                "enabled": True,
                "prompt": shot.get("visual_prompt", ""),
                "caption": shot.get("caption_jp", ""),
                "clip_path": None,
                "source_duration": float(shot["duration_sec"]),
                "trim": {"start": 0.0, "end": float(shot["duration_sec"])},
            }
            if shot.get("image_path"):
                base["image_path"] = shot["image_path"]
            return base

        planned_shots = [_planned_shot(i, shot) for i, shot in enumerate(shots)]
        studio_plan = {
            "shots": planned_shots,
            "narration_text": plan.get("narration_script", ""),
            "bgm": {"file": bgm_file, "gain_db": -14.0, "ducking": True} if bgm_file else None,
            "sfx": [],
            "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE, preset=style),
        }
        # 音声主導タイミング同期モード用: shots[].narration_jp（あれば）をショットidをキーに
        # project直下（plan.shotsの外）に保存する。plan.shotsは以後 PUT /api/projects/{id}/plan
        # -> projects.validate_plan() の既知キーのみへ正規化されnarration_jpは残らないため、
        # project直下に持たせることでStudioでの再編集・再レンダリング後も同期モードが
        # 維持されるようにする（narration_jpが無い/一部欠けているショットがあれば
        # _render_project側の判定で自動的に全文方式へフォールバックする）。
        narration_segments = {}
        for shot in shots:
            njp = shot.get("narration_jp")
            if isinstance(njp, str) and njp.strip():
                narration_segments[shot["id"]] = njp.strip()
        project = projects.get_project(project_id)
        if project is not None:
            project["plan"] = studio_plan
            project["narration_segments"] = narration_segments
            if bgm_file:
                project["bgm_selected"] = bgm_file
            # 編集レシピ選択の入力として bgm_mood を残す（studio_plan には mood が
            # 含まれないため、project 直下に保存する。_render_project はここを読む）。
            _mood = plan.get("bgm_mood")
            if _mood:
                project["bgm_mood"] = _mood
            projects.save_project(project)

        # --- シーングループ（クリップ再利用）で生成する ---------------------------------
        # director が連続ショットへ同じ scene_id を付けている場合、それらを1本の「シーン
        # マスター」（構成ショットの尺の合計）だけ生成し、各ショットへ切り出して再利用する
        # （＝生成は1回・クレジットは1本ぶん）。合計尺が_SCENE_MAX_REQUEST_SECを超える
        # シーンはショット境界で貪欲分割する。scene_id 無しの単独シーン（1メンバー）は
        # 従来どおり直接 clips/<id>.mp4 を生成する（＝全ショットが scene_id 無しなら従来経路と等価）。
        raw_scenes = scenes_mod.group_shots_into_scenes(shots)
        scene_groups = []
        for _sc in raw_scenes:
            scene_groups.extend(scenes_mod.split_scene_by_max_request(_sc, max_sec=_SCENE_MAX_REQUEST_SEC))
        num_scenes = len(scene_groups)
        num_shots = len(shots)
        planned_by_id = {ps["id"]: ps for ps in planned_shots}
        ffmpeg_bin = cfg["ffmpeg_bin"]
        ffprobe_bin = cfg.get("ffprobe_bin") or "ffprobe"
        project_scenes = []
        cut_done = 0

        self._emit(job_id, "visual", 25, "シーンを生成中…(0/{})".format(num_scenes))
        try:
            backend = get_backend(backend_name, cfg)
        except Exception as exc:
            fail("ビジュアルバックエンドの初期化に失敗しました: {}".format(exc))
            return

        def _persist_clip_path(shot_id, filename):
            planned_by_id[shot_id]["clip_path"] = projects.media_relpath_for_clip(project_id, filename)
            proj = projects.get_project(project_id)
            if proj is not None:
                proj["plan"] = studio_plan
                projects.save_project(proj)

        for n, scene in enumerate(scene_groups):
            members = scene["shots"]
            scene_key = scene["scene_key"]
            member_ids = [m["id"] for m in members]
            self._emit(
                job_id, "visual", 25 + int(35 * n / max(1, num_scenes)),
                "シーンを生成中…({}/{})".format(n + 1, num_scenes),
            )

            if len(members) == 1:
                # 単独シーン = 従来経路と等価（シーンマスターを作らず直接 clips/<id>.mp4 を生成）。
                shot = members[0]
                shot_id = shot["id"]
                raw_path = clips_dir / "{}.raw.mp4".format(shot_id)
                norm_path = clips_dir / "{}.mp4".format(shot_id)
                try:
                    backend.generate(shot, str(raw_path))
                    cmd = render.build_normalize_clip_cmd(
                        ffmpeg_bin, str(raw_path), str(norm_path), duration_sec=shot["duration_sec"]
                    )
                    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                    if res["returncode"] != 0:
                        raise RuntimeError(res["stderr"][-500:])
                except Exception as exc:
                    fail("ショット{}の生成に失敗しました: {}".format(shot_id, exc))
                    return
                finally:
                    try:
                        if raw_path.exists():
                            raw_path.unlink()
                    except Exception:
                        pass
                _persist_clip_path(shot_id, "{}.mp4".format(shot_id))
                cut_done += 1
                self._emit(
                    job_id, "visual", 25 + int(45 * cut_done / max(1, num_shots)),
                    "ショットを切り出し中…({}/{})".format(cut_done, num_shots),
                )
                continue

            # --- 複数メンバーのシーン: マスター1本を生成し各ショットへ切り出す ---
            member_durations = [float(m["duration_sec"]) for m in members]
            planned_total = sum(member_durations)
            # plan_schema.validate_plan で visual_prompt 不一致はハードエラーになるため
            # ここに到達した時点で通常は不一致は起こらない。念のため防御としてチェックし、
            # 万一発生していたら警告emitのうえ先頭promptで続行する（生成は止めない）。
            first_prompt = members[0].get("visual_prompt", "")
            mismatched = [m["id"] for m in members[1:] if m.get("visual_prompt", "") != first_prompt]
            if mismatched:
                self._emit(
                    job_id, "visual", 25 + int(35 * n / max(1, num_scenes)),
                    "警告: シーン{}内でvisual_promptが不一致(対象ショット: {})。先頭promptで生成します".format(
                        scene_key, ", ".join(mismatched)
                    ),
                )
            scene_shot = {
                "id": scene_key,
                "visual_prompt": members[0].get("visual_prompt", ""),
                "motion_preset": members[0].get("motion_preset", "static"),
                "duration_sec": planned_total,
                "caption_jp": members[0].get("caption_jp", ""),
            }
            if members[0].get("image_path"):
                scene_shot["image_path"] = members[0]["image_path"]

            safe_key = _safe_scene_key(scene_key)
            scene_fname = "scene__{}.mp4".format(safe_key)
            raw_path = clips_dir / "scene__{}.raw.mp4".format(safe_key)
            master_path = clips_dir / scene_fname
            try:
                with _scaled_credit_limit(backend, len(members)):
                    backend.generate(scene_shot, str(raw_path))
                cmd = render.build_normalize_clip_cmd(
                    ffmpeg_bin, str(raw_path), str(master_path), duration_sec=planned_total
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise RuntimeError(res["stderr"][-500:])
            except Exception as exc:
                fail("シーン{}の生成に失敗しました（対象ショット: {}）: {}".format(
                    scene_key, ", ".join(member_ids), exc))
                return
            finally:
                try:
                    if raw_path.exists():
                        raw_path.unlink()
                except Exception:
                    pass

            actual = _probe_duration(ffprobe_bin, master_path)
            if not actual or actual <= 0:
                actual = planned_total
            windows = scenes_mod.compute_scene_windows(member_durations, actual)

            try:
                for m, win in zip(members, windows):
                    shot_id = m["id"]
                    out_path = clips_dir / "{}.mp4".format(shot_id)
                    cmd = render.build_normalize_clip_cmd(
                        ffmpeg_bin, str(master_path), str(out_path),
                        duration_sec=max(win["content_duration"], _MIN_CUT_SEC),
                        trim_start=win["trim_start"], pad_to_duration_sec=win["pad_to"],
                    )
                    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                    if res["returncode"] != 0:
                        raise RuntimeError(res["stderr"][-500:])
                    _persist_clip_path(shot_id, "{}.mp4".format(shot_id))
                    cut_done += 1
                    self._emit(
                        job_id, "visual", 25 + int(45 * cut_done / max(1, num_shots)),
                        "ショットを切り出し中…({}/{})".format(cut_done, num_shots),
                    )
            except Exception as exc:
                fail("シーン{}のショット切り出しに失敗しました（対象ショット: {}）: {}".format(
                    scene_key, ", ".join(member_ids), exc))
                return

            project_scenes.append({
                "scene_key": scene_key,
                "shot_ids": member_ids,
                # member_durations: 生成時点の各メンバーの計画尺のスナップショット。
                # resume で compute_scene_windows() を再計算するとき、plan.shots から
                # source_duration を読み直すと Studio でのショット削除/trim編集の影響で
                # 窓が生成時と変わってしまい「別の絵の区間」が切り出されてしまう。
                # 生成時の窓を維持するためここでスナップショットを固定する（[高]バグ修正）。
                "member_durations": [float(d) for d in member_durations],
                "visual_prompt": scene_shot["visual_prompt"],
                "motion_preset": scene_shot["motion_preset"],
                "clip_path": projects.media_relpath_for_clip(project_id, scene_fname),
                "planned_duration_sec": planned_total,
                "actual_duration_sec": actual,
            })
            proj = projects.get_project(project_id)
            if proj is not None:
                proj["scenes"] = project_scenes
                projects.save_project(proj)

        # studio_plan["shots"] は planned_shots への参照のため、ここまでのループで
        # 全ショットのclip_pathが確定済み（= 作り直す必要はない）。
        project = projects.get_project(project_id)
        project["plan"] = studio_plan
        project["status"] = "rendering"
        projects.save_project(project)

        self._emit(job_id, "tts", 75, "ナレーションを生成中…")
        self._emit(job_id, "subtitles", 80, "字幕を生成中…")
        self._emit(job_id, "render", 85, "レンダリング中…")
        try:
            out_path, out_duration, tts_meta = _render_project(project_id, studio_plan, cfg)
        except Exception as exc:
            fail("初回レンダリングに失敗しました: {}".format(exc))
            return

        project = projects.get_project(project_id)
        drift_sec, drift_warning = _duration_drift_info(out_duration, project.get("target_duration_sec"))
        if drift_sec is not None:
            tts_meta["duration_drift_sec"] = drift_sec
        if drift_warning:
            self._emit(job_id, "render", 99, drift_warning)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
        project["tts"] = tts_meta
        project["renders"] = (project.get("renders") or []) + [
            {"path": out_path, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}
        ]
        projects.save_project(project)

        self._emit(job_id, "render", 100, "完成しました（尺 {:.1f}秒）".format(out_duration))
        self._finish(job_id, ok=True, path=out_path)

    # --- render（再編集後の再レンダリング） ------------------------------

    def _run_render(self, job_id, payload):
        project_id = payload["project_id"]
        cfg = self.cfg

        def fail(message):
            project = projects.get_project(project_id)
            if project is not None:
                project["status"] = "failed"
                project["error"] = message
                projects.append_error_history(project, "render", message)
                projects.save_project(project)
            self._emit(job_id, "error", 100, message)
            self._finish(job_id, ok=False, path=None)

        project = projects.get_project(project_id)
        if project is None:
            fail("プロジェクトが見つかりません: {}".format(project_id))
            return

        self._emit(job_id, "validate", 5, "編集内容を検証中…")
        ng_words = list(compliance.DEFAULT_NG_WORDS)
        for w in (cfg.get("brand_rules") or {}).get("ng_words") or []:
            if w not in ng_words:
                ng_words.append(w)
        ng_patterns = None
        if project.get("product"):
            # 商品アフィリエイト動画モードのプロジェクト（初回generate時にproductが設定済み）の
            # 再レンダリングでも薬機法検査を効かせる（通常モードのプロジェクトはproduct=Noneのため不変）。
            for w in compliance.BEAUTY_YAKKIHO_NG_WORDS:
                if w not in ng_words:
                    ng_words.append(w)
            ng_patterns = compliance.BEAUTY_YAKKIHO_NG_PATTERNS
        ok, errors, normalized = projects.validate_plan(
            project_id, project.get("plan"), ng_words=ng_words, ng_patterns=ng_patterns
        )
        if not ok:
            fail("編集内容の検証に失敗しました: {}".format("; ".join(errors)))
            return

        # try_start_render() の受理時点と、このワーカーが実際に実行される時点との間に
        # PUT /plan で有効ショットの clip_path が null に書き換えられるレースがあり得る
        # （validate_plan は clip_path=None を許容するため、これ自体は検証を通ってしまう）。
        # ここで再度チェックし、TypeError（Path(None)）として_render_projectの奥深くで
        # 素の例外を出す前に、明確な失敗メッセージでジョブを止める。
        unrendered_ids = projects.unrendered_enabled_shot_ids(normalized)
        if unrendered_ids:
            fail("クリップが未生成の有効なショットがあります: {}".format(", ".join(unrendered_ids)))
            return

        project["status"] = "rendering"
        projects.save_project(project)

        self._emit(job_id, "trim", 15, "ショットをトリム中…")
        self._emit(job_id, "tts", 35, "ナレーションを生成中…")
        self._emit(job_id, "subtitles", 50, "字幕を生成中…")
        self._emit(job_id, "render", 65, "レンダリング中…")
        try:
            out_path, out_duration, tts_meta = _render_project(project_id, normalized, cfg)
        except Exception as exc:
            fail("レンダリングに失敗しました: {}".format(exc))
            return

        project = projects.get_project(project_id)
        drift_sec, drift_warning = _duration_drift_info(out_duration, project.get("target_duration_sec"))
        if drift_sec is not None:
            tts_meta["duration_drift_sec"] = drift_sec
        if drift_warning:
            self._emit(job_id, "render", 99, drift_warning)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
        project["tts"] = tts_meta
        project["renders"] = (project.get("renders") or []) + [
            {"path": out_path, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}
        ]
        projects.save_project(project)

        self._emit(job_id, "render", 100, "完成しました（尺 {:.1f}秒）".format(out_duration))
        self._finish(job_id, ok=True, path=out_path)

    # --- resume（続きから生成: 未生成ショットだけ作り直して最後まで進める） ----------

    def _run_resume(self, job_id, payload):
        """failed状態のプロジェクトについて、clip_path未生成の有効ショットだけを生成し直し、
        揃ったら_render_projectで書き出しまで進める（クレジット消費済みの既存クリップは再生成しない）。
        """
        project_id = payload["project_id"]
        cfg = self.cfg

        def fail(message):
            project = projects.get_project(project_id)
            if project is not None:
                project["status"] = "failed"
                project["error"] = message
                projects.append_error_history(project, "resume", message)
                projects.save_project(project)
            self._emit(job_id, "error", 100, message)
            self._finish(job_id, ok=False, path=None)

        project = projects.get_project(project_id)
        if project is None:
            fail("プロジェクトが見つかりません: {}".format(project_id))
            return

        plan = project.get("plan") or {}
        shots = plan.get("shots") or []
        pending_ids = set(projects.unrendered_enabled_shot_ids(plan))
        shot_by_id = {s.get("id"): s for s in shots}

        ffmpeg_bin = cfg["ffmpeg_bin"]
        ffprobe_bin = cfg.get("ffprobe_bin") or "ffprobe"
        clips_dir = projects.clips_dir(project_id)
        clips_dir.mkdir(parents=True, exist_ok=True)

        # project["scenes"]（初回generateがシーングループで作った場合のみ存在）で pending
        # ショットをグルーピングする。シーンマスターが実在すれば backend を呼ばず再切り出し
        # だけ行う（クレジット消費0）。無ければシーン単位で再生成する。scenes に載っていない
        # ショット（旧plan / scene_id無し）は従来の1ショット経路で個別生成する（混在OK）。
        scenes_meta = project.get("scenes") or []
        scene_by_shot = {}
        for sc in scenes_meta:
            for sid in sc.get("shot_ids") or []:
                scene_by_shot[sid] = sc

        total_pending = len(pending_ids)
        self._emit(job_id, "visual", 25, "続きのショットを生成中… (0/{})".format(total_pending))
        try:
            backend = get_backend(project.get("backend") or "mock", cfg)
        except Exception as exc:
            fail("ビジュアルバックエンドの初期化に失敗しました: {}".format(exc))
            return

        succeeded_ids = []

        def _persist_clip_path(shot_id):
            current = projects.get_project(project_id)
            if current is not None:
                for s in (current.get("plan") or {}).get("shots") or []:
                    if s.get("id") == shot_id:
                        s["clip_path"] = projects.media_relpath_for_clip(project_id, "{}.mp4".format(shot_id))
                projects.save_project(current)

        def _emit_progress():
            self._emit(
                job_id, "visual", 25 + int(45 * len(succeeded_ids) / max(1, total_pending)),
                "続きのショットを生成中… ({}/{})".format(len(succeeded_ids), total_pending),
            )

        # (1) シーン単位の再開: pending メンバーを含むシーンを、scenes_meta の順で処理する。
        for sc in scenes_meta:
            member_ids = list(sc.get("shot_ids") or [])
            pending_members = [sid for sid in member_ids if sid in pending_ids]
            if not pending_members:
                continue
            member_shots = [shot_by_id.get(sid) or {} for sid in member_ids]
            # 生成時に保存された member_durations を優先して使う（[高]バグ修正: シーン窓破壊）。
            # 旧プロジェクト（member_durations 未保存）は plan.shots の source_duration から復元する。
            saved_member_durations = sc.get("member_durations")
            if isinstance(saved_member_durations, list) and len(saved_member_durations) == len(member_ids):
                member_durations = [float(d) for d in saved_member_durations]
            else:
                member_durations = [float(ms.get("source_duration") or 0.0) for ms in member_shots]
            planned_total = float(sc.get("planned_duration_sec") or sum(member_durations))
            master_rel = sc.get("clip_path")
            master_path = projects.resolve_clip_path(project_id, master_rel) if master_rel else None
            master_exists = bool(master_path) and Path(master_path).exists()

            if not master_exists:
                # シーンマスターが失われている: シーン単位で再生成する（クレジット1本ぶん）。
                scene_shot = {
                    "id": sc.get("scene_key") or member_ids[0],
                    "visual_prompt": sc.get("visual_prompt") or (member_shots[0].get("prompt") or ""),
                    "motion_preset": sc.get("motion_preset") or "static",
                    "duration_sec": planned_total,
                    "caption_jp": member_shots[0].get("caption") or "",
                }
                if member_shots[0].get("image_path"):
                    scene_shot["image_path"] = member_shots[0]["image_path"]
                safe_key = _safe_scene_key(sc.get("scene_key") or member_ids[0])
                raw_path = clips_dir / "scene__{}.raw.mp4".format(safe_key)
                master_path = str(clips_dir / "scene__{}.mp4".format(safe_key))
                try:
                    with _scaled_credit_limit(backend, len(member_ids)):
                        backend.generate(scene_shot, str(raw_path))
                    cmd = render.build_normalize_clip_cmd(
                        ffmpeg_bin, str(raw_path), master_path, duration_sec=planned_total
                    )
                    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                    if res["returncode"] != 0:
                        raise RuntimeError(res["stderr"][-500:])
                except Exception as exc:
                    fail("シーン{}の再生成に失敗しました（対象ショット: {}）。ここまでに成功: {}. エラー: {}".format(
                        sc.get("scene_key"), ", ".join(pending_members),
                        ", ".join(succeeded_ids) if succeeded_ids else "なし", exc))
                    return
                finally:
                    try:
                        if raw_path.exists():
                            raw_path.unlink()
                    except Exception:
                        pass
                actual = _probe_duration(ffprobe_bin, master_path)
                if not actual or actual <= 0:
                    actual = planned_total
            else:
                # マスター実在: 再生成せず既存の実尺で切り出すだけ（クレジット0）。
                actual = float(sc.get("actual_duration_sec") or planned_total)

            windows = scenes_mod.compute_scene_windows(member_durations, actual)
            win_by_id = dict(zip(member_ids, windows))
            try:
                for sid in pending_members:
                    win = win_by_id[sid]
                    out_path = clips_dir / "{}.mp4".format(sid)
                    cmd = render.build_normalize_clip_cmd(
                        ffmpeg_bin, str(master_path), str(out_path),
                        duration_sec=max(win["content_duration"], _MIN_CUT_SEC),
                        trim_start=win["trim_start"], pad_to_duration_sec=win["pad_to"],
                    )
                    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                    if res["returncode"] != 0:
                        raise RuntimeError(res["stderr"][-500:])
                    _persist_clip_path(sid)
                    succeeded_ids.append(sid)
                    _emit_progress()
            except Exception as exc:
                fail("シーン{}のショット切り出しに失敗しました（対象ショット: {}）。ここまでに成功: {}. エラー: {}".format(
                    sc.get("scene_key"), ", ".join(pending_members),
                    ", ".join(succeeded_ids) if succeeded_ids else "なし", exc))
                return

        # (2) scenes に載っていない pending ショット: 従来の1ショット経路で個別生成する。
        standalone_pending = [
            s for s in shots
            if s.get("id") in pending_ids and s.get("id") not in scene_by_shot
        ]
        for shot in standalone_pending:
            shot_id = shot.get("id")
            # studio plan の shot（id/order/enabled/prompt/caption/clip_path/source_duration/trim）を
            # バックエンドが期待する shot 形へマッピングする（motion_preset は既定"static"で補う）。
            backend_shot = {
                "id": shot_id,
                "visual_prompt": shot.get("prompt") or "",
                "motion_preset": shot.get("motion_preset") or "static",
                "duration_sec": shot.get("source_duration") or 5.0,
                "caption_jp": shot.get("caption") or "",
            }
            if shot.get("image_path"):
                backend_shot["image_path"] = shot.get("image_path")
            raw_path = clips_dir / "{}.raw.mp4".format(shot_id)
            norm_path = clips_dir / "{}.mp4".format(shot_id)
            try:
                backend.generate(backend_shot, str(raw_path))
                cmd = render.build_normalize_clip_cmd(
                    ffmpeg_bin, str(raw_path), str(norm_path), duration_sec=backend_shot["duration_sec"]
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise RuntimeError(res["stderr"][-500:])
            except Exception as exc:
                fail(
                    "ショット{}の生成に失敗しました（続きから生成）。ここまでに成功したショット: {}. エラー: {}".format(
                        shot_id, ", ".join(succeeded_ids) if succeeded_ids else "なし", exc
                    )
                )
                return
            finally:
                try:
                    if raw_path.exists():
                        raw_path.unlink()
                except Exception:
                    pass
            _persist_clip_path(shot_id)
            succeeded_ids.append(shot_id)
            _emit_progress()

        project = projects.get_project(project_id)
        project["status"] = "rendering"
        projects.save_project(project)

        self._emit(job_id, "tts", 75, "ナレーションを生成中…")
        self._emit(job_id, "subtitles", 80, "字幕を生成中…")
        self._emit(job_id, "render", 85, "レンダリング中…")
        try:
            out_path, out_duration, tts_meta = _render_project(project_id, project["plan"], cfg)
        except Exception as exc:
            fail("レンダリングに失敗しました（続きから生成）: {}".format(exc))
            return

        project = projects.get_project(project_id)
        drift_sec, drift_warning = _duration_drift_info(out_duration, project.get("target_duration_sec"))
        if drift_sec is not None:
            tts_meta["duration_drift_sec"] = drift_sec
        if drift_warning:
            self._emit(job_id, "render", 99, drift_warning)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
        project["tts"] = tts_meta
        project["renders"] = (project.get("renders") or []) + [
            {"path": out_path, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}
        ]
        projects.save_project(project)

        self._emit(job_id, "render", 100, "完成しました（尺 {:.1f}秒）".format(out_duration))
        self._finish(job_id, ok=True, path=out_path)


    # --- premiere_export（「Premiereで編集」ボタン: 書き出しパッケージ生成） ----------

    def _run_premiere_export(self, job_id, payload):
        """readyなプロジェクトのplanから、Premiere Pro読み込み用の書き出しパッケージ一式
        （reel.xml/captions.srt/narration.wav/style/README_import.md）を生成する（Phase A）。

        セットアップが整っている環境（premiere.setup_check.check_setup()がready）では、
        続けてpremiere.driver.run_import()でPremiereへの自動インポート（新規プロジェクト作成
        + reel.xml/captions.srt読み込み + キャプショントラック化 + .prproj保存）まで試みる
        （Phase B）。未セットアップ、またはPhase Bが何らかの理由で失敗した場合でも、
        Phase Aのパッケージ自体は有効なのでジョブは常に ok:true で終える
        （＝Phase Aのパッケージ+READMEへ静かにフォールバックする）。

        既存のgenerate/render/resumeと異なり、project.status/renders は変更しない
        （読み取り専用の副産物生成のため、成功・失敗いずれでも"ready"のまま不変）。
        Phase A自体が失敗した場合のみerror_historyに記録する（既存パターンを踏襲しつつ、
        statusは倒さない）。

        try_start_premiere_export()が受理時に_premiere_exports_in_progressへ追加した
        project_idを、成功/失敗/途中の予期しない例外いずれの終了経路でも必ずfinallyで
        解放する（同一project_idへの次回のpremiere_export要求を永久にブロックしないため）。
        """
        project_id = payload["project_id"]

        def fail(message):
            project = projects.get_project(project_id)
            if project is not None:
                projects.append_error_history(project, "premiere_export", message)
                projects.save_project(project)
            self._emit(job_id, "error", 100, message)
            self._finish(job_id, ok=False, path=None)

        try:
            project = projects.get_project(project_id)
            if project is None:
                fail("プロジェクトが見つかりません: {}".format(project_id))
                return

            self._emit(job_id, "package", 10, "書き出しパッケージを準備中…")

            def _progress(pct, message):
                pct_clamped = max(10, min(90, int(pct)))
                self._emit(job_id, "package", pct_clamped, message)

            try:
                result = premiere_package.build_package(project_id, progress_cb=_progress)
            except Exception as exc:
                fail("Premiere書き出しパッケージの作成に失敗しました: {}".format(exc))
                return

            self._emit(job_id, "package", 100, "書き出しが完了しました")

            # --- Phase B: セットアップが整っていればPremiereへ自動インポートを試みる ---
            setup = premiere_setup_check.check_setup()
            auto_result = None
            if setup["ready"]:
                def _premiere_progress(pct, message):
                    pct_clamped = max(90, min(98, int(pct)))
                    self._emit(job_id, "premiere", pct_clamped, message)

                try:
                    auto_result = premiere_driver.run_import(
                        result["package_dir"], project_id, progress_cb=_premiere_progress
                    )
                except Exception as exc:  # run_import自体は例外を捕捉する設計だが、念のため二重防御する
                    auto_result = {
                        "ok": False, "prproj_path": None, "caption_track_created": False,
                        "detail": "Premiere自動インポート中に予期しないエラーが発生しました: {}".format(exc),
                    }

            auto_ok = bool(auto_result and auto_result.get("ok"))

            # premiere_exports履歴（project.jsonへ追記。status/rendersは不変のまま）
            export_record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "package_dir": result["package_dir"],
                "prproj_path": (auto_result or {}).get("prproj_path"),
                "auto": auto_ok,
            }
            project_for_history = projects.get_project(project_id)
            if project_for_history is not None:
                project_for_history["premiere_exports"] = (
                    project_for_history.get("premiere_exports") or []
                ) + [export_record]
                projects.save_project(project_for_history)

            if auto_ok:
                final_message = "Premiereに組み上げました"
            else:
                final_message = "パッケージを書き出しました（READMEから手動で読み込めます）"
            self._emit(job_id, "premiere", 99, final_message)
            self._publish(job_id, {
                "done": True, "ok": True, "path": result["package_dir"],
                "auto": auto_ok, "message": final_message,
            })
        finally:
            with self._project_status_lock:
                self._premiere_exports_in_progress.discard(project_id)


# ---------------------------------------------------------------------------
# レンダリング本体（generate初回レンダリング・render再レンダリングの共通処理）
# ---------------------------------------------------------------------------

def _render_project(project_id, plan, cfg):
    """plan（Studio形式・正規化済み）からffmpegレンダリングを実行し、(出力パス, 出力尺, tts_meta) を返す。

    トリム済みショットの正規化+連結 -> TTS -> ASS再生成(subtitle_style反映) ->
    BGM(gain_db)+SFX(at_sec配置)オーバーレイ -> 最終loudnorm。

    TTSは2モードある:
      - segment（音声主導タイミング同期モード）: project直下のnarration_segments
        （_run_generateが保存。shot id -> narration_jp）が、有効ショット全件について
        非空で揃っている場合にのみ試みる。tts_mod.synthesize_segments()で断片ごとに
        合成し、成功したら各ショットの表示尺 = max(断片実測尺+0.25秒, 1.2秒)を採用する
        （テロップ/クリップ尺もこの値に統一するため、音声=テロップ=ショット境界が
        原理的に一致する）。断片TTSが1つでも失敗したらfullへフォールバックする。
      - full（従来方式）: narration_textの全文を1本のTTSで合成し、各ショットの表示尺は
        従来どおりtrim尺（trim.end - trim.start）を使う。narration_jpが無い旧プロジェクト、
        またはsegment合成が失敗した場合は常にこちら。
    tts_metaはいずれのモードでも {"backend","duration_sec","is_silent","mode",...} を持つ
    （mode以外のキーはfullなら tts_mod.get_tts_backend().synthesize() の返り値そのもの、
    segmentなら synthesize_segments() の結果から組み立てたもの）。
    """
    pdir = projects.project_dir(project_id)
    work_dir = pdir / "_render_work" / (time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6])
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = cfg["ffmpeg_bin"]

    enabled_shots = sorted([s for s in plan["shots"] if s.get("enabled", True)], key=lambda s: s["order"])
    if not enabled_shots:
        raise RuntimeError("有効なショットが1つもありません（すべてenabled=falseです）")

    project_snapshot = projects.get_project(project_id)
    narration_segments_map = (project_snapshot or {}).get("narration_segments") or {}
    sync_enabled = bool(narration_segments_map) and all(
        isinstance(narration_segments_map.get(s["id"]), str) and narration_segments_map.get(s["id"]).strip()
        for s in enabled_shots
    )

    sync_result = None
    if sync_enabled:
        seg_texts = [narration_segments_map[s["id"]] for s in enabled_shots]
        seg_dir = work_dir / "narration_segments"
        sync_result = tts_mod.synthesize_segments(seg_texts, str(seg_dir), cfg, voice=cfg.get("voice", "Kyoko"))
        if not sync_result.get("ok"):
            sync_enabled = False

    # edit enhancement層（カット点SE/パンチイン/BGM音量カーブ/フックのインパクト）。
    # プロファイル読み込み自体が失敗しても従来レンダを継続する（edit_prof=Noneなら以降すべて無効化）。
    backend_name = (project_snapshot or {}).get("backend") or "mock"
    # 編集レシピ選択の入力: project_id を seed、bgm_mood を mood として渡す。
    # bgm_mood が未保存の旧プロジェクトでも load_edit_profile 側で
    # 「default」重みへフォールバックする（後方互換）。
    _bgm_mood_for_recipe = (project_snapshot or {}).get("bgm_mood")
    try:
        edit_prof = edit_profile.load_edit_profile(
            cfg, project_seed=project_id, bgm_mood=_bgm_mood_for_recipe,
        )
    except Exception:
        edit_prof = None

    trimmed_dir = work_dir / "clips"
    trimmed_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []
    telop_shots = []
    segment_specs = []
    cursor = 0.0
    for i, shot in enumerate(enabled_shots):
        src_path = projects.resolve_clip_path(project_id, shot["clip_path"])
        trim_start = shot["trim"]["start"]
        trim_end = shot["trim"]["end"]
        trim_duration = trim_end - trim_start

        if sync_enabled:
            seg = sync_result["segments"][i]
            display_duration = render.compute_synced_shot_duration(seg["duration_sec"])
        else:
            display_duration = trim_duration

        duration_sec, pad_to_duration_sec = render.resolve_normalize_pad_args(
            trim_duration, display_duration if sync_enabled else None
        )
        punch_in_filter = None
        if edit_prof is not None:
            try:
                punch_in_filter = render.resolve_punch_in_filter_for_shot(i, display_duration, edit_prof, backend_name)
            except Exception:
                punch_in_filter = None
        out_path = trimmed_dir / "{}.mp4".format(shot["id"])
        cmd = render.build_normalize_clip_cmd(
            ffmpeg_bin, str(src_path), str(out_path), duration_sec=duration_sec, trim_start=trim_start,
            pad_to_duration_sec=pad_to_duration_sec, punch_in_filter=punch_in_filter,
        )
        res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
        if res["returncode"] != 0:
            raise RuntimeError("ショット{}のトリムに失敗しました: {}".format(shot["id"], res["stderr"][-500:]))
        clip_paths.append(str(out_path))
        telop_shots.append({"id": shot["id"], "duration_sec": display_duration, "caption_jp": shot["caption"]})
        if sync_enabled:
            segment_specs.append({"path": seg["path"], "start_sec": cursor})
        cursor += display_duration

    list_path = work_dir / "list.txt"
    list_path.write_text(render.build_concat_list_content(clip_paths), encoding="utf-8")
    concat_path = work_dir / "concat.mp4"
    cmd = render.build_concat_cmd(ffmpeg_bin, str(list_path), str(concat_path))
    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
    if res["returncode"] != 0:
        raise RuntimeError("連結(concat)に失敗しました: {}".format(res["stderr"][-500:]))

    out_duration = sum(s["duration_sec"] for s in telop_shots)

    narration_path = work_dir / "narration.wav"
    if sync_enabled:
        cmd = render.build_narration_segments_concat_cmd(ffmpeg_bin, segment_specs, out_duration, str(narration_path))
        res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
        if res["returncode"] != 0:
            raise RuntimeError("ナレーション断片の合成に失敗しました: {}".format(res["stderr"][-500:]))
        tts_meta = {
            "backend": sync_result.get("backend"), "duration_sec": out_duration, "is_silent": False,
            "requested_backend": sync_result.get("backend"), "fallback_reason": sync_result.get("fallback_reason"),
            "mode": "segment",
        }
    else:
        tts_backend = tts_mod.get_tts_backend(voice=cfg.get("voice", "Kyoko"), cfg=cfg)
        tts_meta = dict(tts_backend.synthesize(plan.get("narration_text", ""), str(narration_path), cfg))
        tts_meta["mode"] = "full"

    telop_pieces = subtitles.build_telop_pieces_from_shots(telop_shots, hook_shot_id=telop_shots[0]["id"] if telop_shots else None)
    product_name = ((projects.get_project(project_id) or {}).get("product") or {}).get("name")
    # 編集プロファイルの telop.animation=="none" を尊重(参考TikTok風のカットイン表示。
    # プロファイル読込に失敗してもアニメ有効の従来挙動へフォールバック)。
    animation_enabled = True
    try:
        from premiere.profile import load_profile as _load_full_profile
        _profile = _load_full_profile(_TELOP_PROFILE_NAME) or {}
        if ((_profile.get("telop") or {}).get("animation")) == "none":
            animation_enabled = False
    except Exception:
        pass
    # 動画ごとにテロップの見た目バリエーション（フォント/縁/座布団）を決定論選択する。
    # seed=project_id・vertical_hookプリセットは"vertical-serif"固定・それ以外は
    # horizontal_pool(7スタイル)から seed で選ぶ。選択結果は project["telop_style"] に記録し
    # かんたんモードの完成画面に「テロップ: <名前>」1行を表示する。
    _plan_sub_style = subtitles.resolve_subtitle_style(plan.get("subtitle_style"))
    telop_style_name = subtitles.pick_telop_style_name(
        project_id, preset=_plan_sub_style.get("preset"), record_project_id=project_id,
    )
    ass_text = subtitles.generate_ass_with_style(
        telop_pieces, plan.get("subtitle_style"), product_name=product_name,
        animation_enabled=animation_enabled, telop_style=telop_style_name,
    )
    ass_path = work_dir / "subtitles.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    # 選択したテロップスタイルを project.json へ記録する（同じプロジェクトを再レンダしても
    # 同じ結果になる=決定論。UIで名前を出すため display_name も合わせて保存）。
    try:
        _telop_def = subtitles.resolve_telop_style_def(telop_style_name)
        _project = projects.get_project(project_id)
        if _project is not None:
            _project["telop_style"] = {
                "name": telop_style_name,
                "display_name": _telop_def.get("display_name") or telop_style_name,
            }
            projects.save_project(_project)
    except Exception:
        pass

    bgm_cfg = plan.get("bgm")
    bgm_path = None
    bgm_gain_db = None
    bgm_ducking = True
    if bgm_cfg and bgm_cfg.get("file"):
        resolved = projects.resolve_bgm_path(bgm_cfg["file"])
        bgm_path = str(resolved) if resolved else None
        bgm_gain_db = bgm_cfg.get("gain_db")
        bgm_ducking = bgm_cfg.get("ducking", True)

    sfx_specs = []
    for s in plan.get("sfx") or []:
        resolved = projects.resolve_sfx_path(s.get("file"))
        if resolved is None:
            continue
        sfx_specs.append({"path": str(resolved), "at_sec": s.get("at_sec", 0.0), "gain_db": s.get("gain_db", -8.0)})

    # edit enhancement層: カットSEをsfx_specsへ追加し、BGM音量カーブ/フックのインパクトを
    # 求める。この層の計算で例外が起きても従来レンダ(edit無し)へフォールバックする
    # （edit_profile_applied=Falseとして記録するのみで、レンダ全体は落とさない）。
    edit_profile_applied = False
    bgm_curve = None
    first_shot_impact_sec = None
    if edit_prof is not None:
        try:
            durations = [s["duration_sec"] for s in telop_shots]
            enhancement = render.compute_edit_enhancement_kwargs(
                durations, edit_prof, project_seed=project_id,
            )
            sfx_specs = sfx_specs + enhancement["sfx_extra"]
            bgm_curve = enhancement["bgm_curve"]
            first_shot_impact_sec = enhancement["first_shot_impact_sec"]
            edit_profile_applied = True
        except Exception:
            bgm_curve = None
            first_shot_impact_sec = None
            edit_profile_applied = False

    renders_dir = projects.renders_dir(project_id)
    renders_dir.mkdir(parents=True, exist_ok=True)
    output_filename = "{}.mp4".format(time.strftime("%Y%m%d%H%M%S"))
    output_path = renders_dir / output_filename

    pass1_path = work_dir / "_loudnorm_pass1.mp4"
    cmd1 = render.build_final_cmd(
        ffmpeg_bin, str(concat_path), str(narration_path), str(pass1_path), str(ass_path), str(FONTS_DIR),
        bgm_path=bgm_path, out_duration=out_duration, loudnorm_measured=None,
        bgm_gain_db=bgm_gain_db, sfx=sfx_specs, ducking=bgm_ducking,
        bgm_curve=bgm_curve, first_shot_impact_sec=first_shot_impact_sec,
    )
    res1 = render.run_ffmpeg(cmd1, timeout_sec=_FFMPEG_TIMEOUT_SEC)
    measured = _parse_loudnorm_json(res1["stderr"])
    try:
        if pass1_path.exists():
            pass1_path.unlink()
    except Exception:
        pass

    if res1["returncode"] != 0 or measured is None:
        cmd2 = render.build_final_cmd(
            ffmpeg_bin, str(concat_path), str(narration_path), str(output_path), str(ass_path), str(FONTS_DIR),
            bgm_path=bgm_path, out_duration=out_duration, loudnorm_measured=None,
            bgm_gain_db=bgm_gain_db, sfx=sfx_specs, ducking=bgm_ducking,
            bgm_curve=bgm_curve, first_shot_impact_sec=first_shot_impact_sec,
        )
    else:
        cmd2 = render.build_final_cmd(
            ffmpeg_bin, str(concat_path), str(narration_path), str(output_path), str(ass_path), str(FONTS_DIR),
            bgm_path=bgm_path, out_duration=out_duration, loudnorm_measured=measured,
            bgm_gain_db=bgm_gain_db, sfx=sfx_specs, ducking=bgm_ducking,
            bgm_curve=bgm_curve, first_shot_impact_sec=first_shot_impact_sec,
        )
    res2 = render.run_ffmpeg(cmd2, timeout_sec=_FFMPEG_TIMEOUT_SEC)
    if res2["returncode"] != 0:
        raise RuntimeError("最終レンダリングに失敗しました: {}".format(res2["stderr"][-800:]))

    try:
        proj_for_flag = projects.get_project(project_id)
        if proj_for_flag is not None:
            proj_for_flag["edit_profile_applied"] = edit_profile_applied
            # 選択された編集レシピ名（あれば）を project に記録する。
            # 「編集の方向性が毎回一緒」問題への監査ポイント: どの動画が
            # どのレシピで組まれたかを追える。
            if isinstance(edit_prof, dict) and edit_prof.get("edit_recipe"):
                proj_for_flag["edit_recipe"] = edit_prof["edit_recipe"]
            projects.save_project(proj_for_flag)
    except Exception:
        pass

    shutil.rmtree(work_dir, ignore_errors=True)
    return projects.media_relpath_for_render(project_id, output_filename), out_duration, tts_meta


job_manager = JobManager()
