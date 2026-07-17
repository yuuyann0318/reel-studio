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
#   JobManager初期化時のスタック回復でstatus="failed"に倒し、resumeで再開できるようにする。

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

from pipeline import compliance
from pipeline import director
from pipeline import render
from pipeline import subtitles
from pipeline import tts as tts_mod
from pipeline.config import load_config, project_root
from pipeline.visual import get_backend
from studio.server import projects

ASSETS_DIR = project_root() / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
BGM_MANIFEST = ASSETS_DIR / "bgm" / "manifest.json"

_FFMPEG_TIMEOUT_SEC = 1800
_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

TERMINAL = ("done",)  # SSE側は最終イベント {"done":true,...} の送出をもって終端とみなす


def _new_job_id():
    return "job_" + time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]


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


def _resolve_bgm_by_mood(mood):
    if not mood or mood == "none":
        return None
    if not BGM_MANIFEST.exists():
        return None
    try:
        manifest = json.loads(BGM_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return None
    for entry in manifest or []:
        if entry.get("mood") == mood:
            return entry.get("file")
    return None


class RenderConflictError(Exception):
    """既に generating/rendering 中のプロジェクトへの重複render要求。"""


class ResumeNotAllowedError(Exception):
    """failed以外のプロジェクトへのresume要求（再開の必要がない状態）。"""


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
        self._recover_stuck_projects()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    # --- 起動時のスタック回復 -------------------------------------------

    def _recover_stuck_projects(self):
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

    def start_generate(self, project_id, theme, target_duration_sec, backend_name, style="default"):
        job_id = project_id  # POST /api/projects はプロジェクトIDそのものをjob_idとして使う（設計判断）
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "kind": "generate", "status": "queued"}
        self._queue.put(("generate", job_id, {
            "project_id": project_id, "theme": theme,
            "target_duration_sec": target_duration_sec, "backend_name": backend_name,
            "style": style,
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

        self._emit(job_id, "director", 5, "企画を生成中…")
        try:
            plan = director.run_director(theme, cfg, target_duration_sec=target_duration_sec, no_llm=False, style=style)
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
        check = compliance.check_plan(plan, ng_words=ng_words)
        if not check["ok"]:
            fail("コンプライアンス違反のため生成を停止しました: {}".format(check["violations"]))
            return

        shots = plan.get("shots", [])
        pdir = projects.project_dir(project_id)
        clips_dir = projects.clips_dir(project_id)
        clips_dir.mkdir(parents=True, exist_ok=True)

        # ★BUG-10修正: director企画（shotsの内容）は、以降のビジュアル生成が途中で失敗しても
        # project.json から失われてはいけない。ここで先にclip_path=Noneの状態でplanを保存し、
        # ショットが1本も生成できずに失敗しても plan.shots に企画内容（prompt/caption/尺）が
        # 残るようにする。以前は生成ループを抜けた後にまとめて project["plan"] を書いていた
        # ため、途中失敗時は create_project() 時点の空のplan（shots:[]）のままになっていた。
        bgm_file = _resolve_bgm_by_mood(plan.get("bgm_mood"))
        planned_shots = [
            {
                "id": shot["id"],
                "order": i,
                "enabled": True,
                "prompt": shot.get("visual_prompt", ""),
                "caption": shot.get("caption_jp", ""),
                "clip_path": None,
                "source_duration": float(shot["duration_sec"]),
                "trim": {"start": 0.0, "end": float(shot["duration_sec"])},
            }
            for i, shot in enumerate(shots)
        ]
        studio_plan = {
            "shots": planned_shots,
            "narration_text": plan.get("narration_script", ""),
            "bgm": {"file": bgm_file, "gain_db": -14.0, "ducking": True} if bgm_file else None,
            "sfx": [],
            "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE, preset=style),
        }
        project = projects.get_project(project_id)
        if project is not None:
            project["plan"] = studio_plan
            projects.save_project(project)

        self._emit(job_id, "visual", 25, "ショットを生成中… (0/{})".format(len(shots)))
        try:
            backend = get_backend(backend_name, cfg)
        except Exception as exc:
            fail("ビジュアルバックエンドの初期化に失敗しました: {}".format(exc))
            return

        for i, shot in enumerate(shots):
            raw_path = clips_dir / "{}.raw.mp4".format(shot["id"])
            norm_path = clips_dir / "{}.mp4".format(shot["id"])
            try:
                backend.generate(shot, str(raw_path))
                cmd = render.build_normalize_clip_cmd(
                    cfg["ffmpeg_bin"], str(raw_path), str(norm_path), duration_sec=shot["duration_sec"]
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise RuntimeError(res["stderr"][-500:])
            except Exception as exc:
                fail("ショット{}の生成に失敗しました: {}".format(shot.get("id"), exc))
                return
            finally:
                try:
                    if raw_path.exists():
                        raw_path.unlink()
                except Exception:
                    pass

            # このショットの生成が成功したので、その場でclip_pathを確定して保存する
            # （途中で後続ショットが失敗しても、ここまでの成功分は失われない）。
            planned_shots[i]["clip_path"] = projects.media_relpath_for_clip(
                project_id, "{}.mp4".format(shot["id"])
            )
            project = projects.get_project(project_id)
            if project is not None:
                project["plan"] = studio_plan
                projects.save_project(project)
            progress = 25 + int(45 * (i + 1) / max(1, len(shots)))
            self._emit(job_id, "visual", progress, "ショットを生成中… ({}/{})".format(i + 1, len(shots)))

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
            out_path, out_duration = _render_project(project_id, studio_plan, cfg)
        except Exception as exc:
            fail("初回レンダリングに失敗しました: {}".format(exc))
            return

        project = projects.get_project(project_id)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
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
        ok, errors, normalized = projects.validate_plan(project_id, project.get("plan"), ng_words=ng_words)
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
            out_path, out_duration = _render_project(project_id, normalized, cfg)
        except Exception as exc:
            fail("レンダリングに失敗しました: {}".format(exc))
            return

        project = projects.get_project(project_id)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
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
        pending_shots = [s for s in shots if s.get("id") in pending_ids]

        clips_dir = projects.clips_dir(project_id)
        clips_dir.mkdir(parents=True, exist_ok=True)

        self._emit(job_id, "visual", 25, "続きのショットを生成中… (0/{})".format(len(pending_shots)))
        try:
            backend = get_backend(project.get("backend") or "mock", cfg)
        except Exception as exc:
            fail("ビジュアルバックエンドの初期化に失敗しました: {}".format(exc))
            return

        succeeded_ids = []
        for i, shot in enumerate(pending_shots):
            shot_id = shot.get("id")
            # studio plan の shot（id/order/enabled/prompt/caption/clip_path/source_duration/trim）を
            # バックエンドが期待する shot 形（id/visual_prompt/motion_preset/duration_sec/caption_jp）へ
            # マッピングする。studio plan には motion_preset が無いため既定値"static"で補う
            # （既存project=本機能追加前に作られたものでも安全に動くようにするための既定値）。
            backend_shot = {
                "id": shot_id,
                "visual_prompt": shot.get("prompt") or "",
                "motion_preset": shot.get("motion_preset") or "static",
                "duration_sec": shot.get("source_duration") or 5.0,
                "caption_jp": shot.get("caption") or "",
            }
            raw_path = clips_dir / "{}.raw.mp4".format(shot_id)
            norm_path = clips_dir / "{}.mp4".format(shot_id)
            try:
                backend.generate(backend_shot, str(raw_path))
                cmd = render.build_normalize_clip_cmd(
                    cfg["ffmpeg_bin"], str(raw_path), str(norm_path), duration_sec=backend_shot["duration_sec"]
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

            # このショットの生成が成功したので、その場でclip_pathを確定して保存する
            # （generateの初回生成と同じ思想: 途中で後続ショットが失敗しても、ここまでの成功分は失われない）。
            current = projects.get_project(project_id)
            if current is not None:
                for s in (current.get("plan") or {}).get("shots") or []:
                    if s.get("id") == shot_id:
                        s["clip_path"] = projects.media_relpath_for_clip(project_id, "{}.mp4".format(shot_id))
                projects.save_project(current)
            succeeded_ids.append(shot_id)
            progress = 25 + int(45 * (i + 1) / max(1, len(pending_shots)))
            self._emit(job_id, "visual", progress, "続きのショットを生成中… ({}/{})".format(i + 1, len(pending_shots)))

        project = projects.get_project(project_id)
        project["status"] = "rendering"
        projects.save_project(project)

        self._emit(job_id, "tts", 75, "ナレーションを生成中…")
        self._emit(job_id, "subtitles", 80, "字幕を生成中…")
        self._emit(job_id, "render", 85, "レンダリング中…")
        try:
            out_path, out_duration = _render_project(project_id, project["plan"], cfg)
        except Exception as exc:
            fail("レンダリングに失敗しました（続きから生成）: {}".format(exc))
            return

        project = projects.get_project(project_id)
        project["status"] = "ready"
        project["error"] = None  # 成功したら最新エラー表示を消す（履歴はerror_historyに残る）
        project["renders"] = (project.get("renders") or []) + [
            {"path": out_path, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}
        ]
        projects.save_project(project)

        self._emit(job_id, "render", 100, "完成しました（尺 {:.1f}秒）".format(out_duration))
        self._finish(job_id, ok=True, path=out_path)


# ---------------------------------------------------------------------------
# レンダリング本体（generate初回レンダリング・render再レンダリングの共通処理）
# ---------------------------------------------------------------------------

def _render_project(project_id, plan, cfg):
    """plan（Studio形式・正規化済み）からffmpegレンダリングを実行し、(出力パス, 出力尺) を返す。

    トリム済みショットの正規化+連結 -> TTS(常に現在のnarration_textから再生成) ->
    ASS再生成(subtitle_style反映) -> BGM(gain_db)+SFX(at_sec配置)オーバーレイ -> 最終loudnorm。
    """
    pdir = projects.project_dir(project_id)
    work_dir = pdir / "_render_work" / (time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6])
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = cfg["ffmpeg_bin"]

    enabled_shots = sorted([s for s in plan["shots"] if s.get("enabled", True)], key=lambda s: s["order"])
    if not enabled_shots:
        raise RuntimeError("有効なショットが1つもありません（すべてenabled=falseです）")

    trimmed_dir = work_dir / "clips"
    trimmed_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []
    telop_shots = []
    for shot in enabled_shots:
        src_path = projects.resolve_clip_path(project_id, shot["clip_path"])
        trim_start = shot["trim"]["start"]
        trim_end = shot["trim"]["end"]
        trim_duration = trim_end - trim_start
        out_path = trimmed_dir / "{}.mp4".format(shot["id"])
        cmd = render.build_normalize_clip_cmd(
            ffmpeg_bin, str(src_path), str(out_path), duration_sec=trim_duration, trim_start=trim_start
        )
        res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
        if res["returncode"] != 0:
            raise RuntimeError("ショット{}のトリムに失敗しました: {}".format(shot["id"], res["stderr"][-500:]))
        clip_paths.append(str(out_path))
        telop_shots.append({"id": shot["id"], "duration_sec": trim_duration, "caption_jp": shot["caption"]})

    list_path = work_dir / "list.txt"
    list_path.write_text(render.build_concat_list_content(clip_paths), encoding="utf-8")
    concat_path = work_dir / "concat.mp4"
    cmd = render.build_concat_cmd(ffmpeg_bin, str(list_path), str(concat_path))
    res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
    if res["returncode"] != 0:
        raise RuntimeError("連結(concat)に失敗しました: {}".format(res["stderr"][-500:]))

    out_duration = sum(s["duration_sec"] for s in telop_shots)

    narration_path = work_dir / "narration.wav"
    tts_backend = tts_mod.get_tts_backend(voice=cfg.get("voice", "Kyoko"))
    tts_backend.synthesize(plan.get("narration_text", ""), str(narration_path), cfg)

    telop_pieces = subtitles.build_telop_pieces_from_shots(telop_shots, hook_shot_id=telop_shots[0]["id"] if telop_shots else None)
    ass_text = subtitles.generate_ass_with_style(telop_pieces, plan.get("subtitle_style"))
    ass_path = work_dir / "subtitles.ass"
    ass_path.write_text(ass_text, encoding="utf-8")

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

    renders_dir = projects.renders_dir(project_id)
    renders_dir.mkdir(parents=True, exist_ok=True)
    output_filename = "{}.mp4".format(time.strftime("%Y%m%d%H%M%S"))
    output_path = renders_dir / output_filename

    pass1_path = work_dir / "_loudnorm_pass1.mp4"
    cmd1 = render.build_final_cmd(
        ffmpeg_bin, str(concat_path), str(narration_path), str(pass1_path), str(ass_path), str(FONTS_DIR),
        bgm_path=bgm_path, out_duration=out_duration, loudnorm_measured=None,
        bgm_gain_db=bgm_gain_db, sfx=sfx_specs, ducking=bgm_ducking,
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
        )
    else:
        cmd2 = render.build_final_cmd(
            ffmpeg_bin, str(concat_path), str(narration_path), str(output_path), str(ass_path), str(FONTS_DIR),
            bgm_path=bgm_path, out_duration=out_duration, loudnorm_measured=measured,
            bgm_gain_db=bgm_gain_db, sfx=sfx_specs, ducking=bgm_ducking,
        )
    res2 = render.run_ffmpeg(cmd2, timeout_sec=_FFMPEG_TIMEOUT_SEC)
    if res2["returncode"] != 0:
        raise RuntimeError("最終レンダリングに失敗しました: {}".format(res2["stderr"][-800:]))

    shutil.rmtree(work_dir, ignore_errors=True)
    return projects.media_relpath_for_render(project_id, output_filename), out_duration


job_manager = JobManager()
