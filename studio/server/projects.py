# -*- coding: utf-8 -*-
"""Reel Studio: プロジェクト永続化（projects/<id>/project.json）。

docs/STUDIO-DESIGN.md のデータモデルに準拠。バックエンド側のみを実装する
（studio/web/ はフロント担当が並行実装中のため触れない）。

project.json:
{
  "id","theme","created_at","status": "draft|generating|ready|rendering|failed",
  "backend","target_duration_sec",
  "plan": {
    "shots": [{"id","order","enabled","prompt","caption","clip_path","source_duration","trim":{"start","end"}}],
    "narration_text","bgm": {"file","gain_db","ducking"}|None,
    "sfx": [{"file","at_sec","gain_db"}],
    "subtitle_style": {"font_size","accent_color","position"},
  },
  "renders": [{"path","ts","ok"}],
  "error": str|None,
  "error_history": [{"ts","kind":"generate"|"render"|"resume","message"}],
  "director": {"model_used","source","quality"}|None,
  "reference": {"url","ok","source","cached","duration_sec","beats_count","warnings"}|
               {"url","ok":False,"error"}|None
}

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from pipeline import compliance
from pipeline.config import project_root

PROJECTS_ROOT = project_root() / "projects"
ASSETS_DIR = project_root() / "assets"
BGM_DIR = ASSETS_DIR / "bgm"
SFX_DIR = ASSETS_DIR / "sfx"
BGM_MANIFEST = BGM_DIR / "manifest.json"
SFX_MANIFEST = SFX_DIR / "manifest.json"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_SUBTITLE_STYLE = {"font_size": 76, "accent_color": "#FFD84D", "position": "lower", "preset": "default"}
VALID_POSITIONS = ("lower", "center", "upper")  # studio/web/components/inspector.js のselect値と一致させる
VALID_SUBTITLE_PRESETS = ("default", "vertical_hook")  # pipeline/subtitles.py SUBTITLE_STYLE_PRESETSと一致させる
STATUSES = ("draft", "generating", "ready", "rendering", "failed")


class ProjectNotFound(Exception):
    pass


class PlanValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class UnsafeMediaPathError(ValueError):
    """resolve_media_relpath/resolve_clip_pathの解決結果がPROJECTS_ROOT配下から
    逸脱した場合に送出する（'../'によるディレクトリトラバーサル・シンボリックリンク
    経由の脱出・PROJECTS_ROOT外を指す絶対パスを検知するため）。
    """


def is_safe_project_id(project_id):
    return bool(project_id) and bool(_ID_RE.match(project_id))


def new_project_id():
    return "p_" + time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]


def project_dir(project_id):
    return PROJECTS_ROOT / project_id


def clips_dir(project_id):
    return project_dir(project_id) / "clips"


def renders_dir(project_id):
    return project_dir(project_id) / "renders"


def product_dir(project_id):
    """商品モードで取得した商品画像の保存先（project_dir(project_id)/product）。"""
    return project_dir(project_id) / "product"


def _project_json_path(project_id):
    return project_dir(project_id) / "project.json"


def media_relpath_for_clip(project_id, filename):
    """クリップの正規パス（project_root()相対・/media/projects/... とディスク解決の両方に使う共通形式）。"""
    return "projects/{}/clips/{}".format(project_id, filename)


def media_relpath_for_render(project_id, filename):
    """レンダー結果の正規パス（project_root()相対）。"""
    return "projects/{}/renders/{}".format(project_id, filename)


def media_relpath_for_product(project_id, filename):
    """商品画像の正規パス（project_root()相対・/media/projects/... で配信するため）。"""
    return "projects/{}/product/{}".format(project_id, filename)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def create_project(theme, target_duration_sec, backend_name, status="generating", style="default", product_url=None,
                    reference_url=None, plan_tier=None, billing=None):
    """新規プロジェクトの雛形を作成し保存する。plan/shotsは生成ジョブが後で埋める。

    style: "default" | "vertical_hook"（縦書きテロップ・高速カットのTTPスタイル）。
    directorの企画生成（shot数/尺の目安/構成）と、生成後のplan.subtitle_style.presetの
    両方に反映される。不明な値は"default"に正規化する。
    product_url: 商品アフィリエイト動画モードの入口（任意）。実際の商品情報・取得画像は
    生成ジョブ（jobs.py _run_generate）が collect_product_images 実行後に project["product"]
    へ書き込む。ここでは常に None で初期化する（呼び出し側の透過渡しのためだけに受け取る）。
    reference_url: 参考動画リンクの入口（任意）。実際の解析結果（pipeline.reference.analyze_reference
    の戻り値の要約）は生成ジョブ（jobs.py _run_generate）が解析実行後に project["reference"]
    へ書き込む（成功/失敗いずれもfail-openで書き込む）。ここでは常に None で初期化する
    （product_urlと同じく、呼び出し側の透過渡しのためだけに受け取る）。
    """
    style = style if style in VALID_SUBTITLE_PRESETS else "default"
    project_id = new_project_id()
    pdir = project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=True)
    clips_dir(project_id).mkdir(parents=True, exist_ok=True)
    renders_dir(project_id).mkdir(parents=True, exist_ok=True)

    project = {
        "id": project_id,
        "theme": theme,
        "created_at": _now_iso(),
        "status": status,
        "backend": backend_name,
        # plan_tier: "free"（むりょうコース） | "paid"（本番コース）。UI の2択に対応する
        # 1変数。None（旧プロジェクト/インポート）のときは backend から後方互換で推定して表示する。
        "plan_tier": plan_tier,
        # billing: 開始前の費用見積と実消費の記録（コース/消費実績の表示・見積精度改善用）。
        "billing": billing,
        "target_duration_sec": target_duration_sec,
        "style": style,
        "plan": {
            "shots": [],
            "narration_text": "",
            "bgm": None,
            "sfx": [],
            "subtitle_style": dict(DEFAULT_SUBTITLE_STYLE, preset=style),
        },
        "renders": [],
        "error": None,
        "error_history": [],
        "director": None,
        "product": None,
        "reference": None,
        "tts": None,
    }
    save_project(project)
    return project


def append_error_history(project, kind, message):
    """project辞書に失敗履歴を追記する（project["error"]の上書きで元の原因が消えないようにする）。

    project["error"]は常に「直近の」失敗理由を指すが、error_historyは追記のみで
    上書きしない。呼び出し側は追記後にsave_project()すること（このヘルパー自体は保存しない）。
    """
    history = project.setdefault("error_history", [])
    history.append({"ts": _now_iso(), "kind": kind, "message": message})
    return project


def save_project(project):
    pdir = project_dir(project["id"])
    pdir.mkdir(parents=True, exist_ok=True)
    _project_json_path(project["id"]).write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return project


def get_project(project_id):
    if not is_safe_project_id(project_id):
        return None
    p = _project_json_path(project_id)
    if not p.exists():
        return None
    try:
        project = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 旧project.json（本機能追加前に作成されたもの）にはerror_history/director/product/reference/ttsが
    # 無いため、既定値を補って呼び出し側が常に安全に扱えるようにする。
    project.setdefault("error_history", [])
    project.setdefault("director", None)
    project.setdefault("product", None)
    project.setdefault("reference", None)
    project.setdefault("tts", None)
    # ②検知: 映像内文字/文字化けの疑いを記録する text_artifacts（本機能追加前の
    # プロジェクトには無いため空配列で補い、UI が常に安全に扱えるようにする）。
    project.setdefault("text_artifacts", [])
    return project


def list_projects():
    """一覧用の要約 {id,theme,status,thumb,created_at} を新しい順で返す。"""
    if not PROJECTS_ROOT.exists():
        return []
    summaries = []
    for child in PROJECTS_ROOT.iterdir():
        if not child.is_dir():
            continue
        project = get_project(child.name)
        if project is None:
            continue
        thumb = None
        shots = (project.get("plan") or {}).get("shots") or []
        enabled_shots = [s for s in shots if s.get("enabled", True)]
        target_shots = enabled_shots or shots
        if target_shots:
            first = sorted(target_shots, key=lambda s: s.get("order", 0))[0]
            thumb = first.get("clip_path")
        summaries.append({
            "id": project["id"],
            "theme": project.get("theme"),
            "status": project.get("status"),
            "thumb": thumb,
            "created_at": project.get("created_at"),
        })
    summaries.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return summaries


def delete_project_dir_if_failed_before_save(project_id):
    """雛形作成後、致命的な初期化エラーが起きた場合の後始末（存在すれば削除）。"""
    import shutil
    d = project_dir(project_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# plan検証（PUT /api/projects/{id}/plan・render前の防御的再検証で共通利用）
# ---------------------------------------------------------------------------

def _load_manifest(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def resolve_media_relpath(rel_path):
    """project.jsonに保存する正規形式のメディアパス（例: 'projects/<id>/clips/s1.mp4'・
    'projects/<id>/renders/xxx.mp4'）を絶対パスへ解決する。

    フロント側の `mediaUrl("raw", file)` が `/media/${file}` をそのまま組み立てる契約のため、
    project.json に保存するclip_path/renders[].pathは常にこの形式で統一する
    （= ディスク解決とURL構築が同じ文字列で成立する）。
    PROJECTS_ROOT（モジュール属性）を都度参照するため、テストでのmonkeypatchにも追従する。

    セキュリティ: PUT /api/projects/{id}/plan 経由でclip_path等はクライアントから
    任意文字列を受け取れるため、ここで解決結果がPROJECTS_ROOT配下に収まることを
    Path.resolve()ベースで検証する（'../../'による脱出・シンボリックリンク経由の
    脱出・PROJECTS_ROOT外を指す絶対パスの指定を検知しUnsafeMediaPathErrorで拒否する）。
    /media/projects StaticFilesマウントがPROJECTS_ROOTそのものを配信しているため、
    許容範囲の境界はPROJECTS_ROOTとする。
    """
    if not rel_path:
        raise UnsafeMediaPathError("rel_path が空です")
    p = Path(rel_path)
    root = PROJECTS_ROOT.parent
    candidate = p if p.is_absolute() else (root / p)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise UnsafeMediaPathError(
            "パスの解決に失敗しました（循環シンボリックリンク等の疑い）: {!r} ({})".format(rel_path, exc)
        )
    projects_root_resolved = PROJECTS_ROOT.resolve(strict=False)
    if resolved != projects_root_resolved and projects_root_resolved not in resolved.parents:
        raise UnsafeMediaPathError(
            "パスがprojects/配下から逸脱しています（パストラバーサルの疑い）: {!r}".format(rel_path)
        )
    return resolved


def resolve_clip_path(project_id, clip_path):
    """clip_pathを絶対パスへ解決する（project_id引数は将来の後方互換用に残す）。

    PROJECTS_ROOT外を指す場合は UnsafeMediaPathError を送出する
    （resolve_media_relpath参照）。
    """
    return resolve_media_relpath(clip_path)


def _resolve_within_dir(base_dir, filename):
    """filenameをbase_dir配下に解決し、境界逸脱を検知する共通ヘルパー
    （resolve_media_relpathと同型の検証: '../'によるトラバーサル・境界外を指す
    絶対パス・シンボリックリンク経由の脱出をUnsafeMediaPathErrorで拒否する）。

    実在チェックは呼び出し側の責務（ここではパス解決と境界検証のみ行う）。
    """
    p = Path(filename)
    candidate = p if p.is_absolute() else (base_dir / p)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise UnsafeMediaPathError(
            "パスの解決に失敗しました（循環シンボリックリンク等の疑い）: {!r} ({})".format(filename, exc)
        )
    base_resolved = base_dir.resolve(strict=False)
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise UnsafeMediaPathError(
            "パスが{}配下から逸脱しています（パストラバーサルの疑い）: {!r}".format(base_dir.name, filename)
        )
    return resolved


def resolve_bgm_path(filename):
    """filenameをBGM_DIR配下の絶対パスへ解決する。

    BGM_DIR外を指す場合はUnsafeMediaPathErrorを送出する（_resolve_within_dir参照）。
    ファイルが存在しない場合はNoneを返す（従来どおりの契約を維持）。
    """
    if not filename:
        return None
    p = _resolve_within_dir(BGM_DIR, filename)
    return p if p.exists() else None


def resolve_sfx_path(filename):
    """filenameをSFX_DIR配下の絶対パスへ解決する。

    SFX_DIR外を指す場合はUnsafeMediaPathErrorを送出する（_resolve_within_dir参照）。
    ファイルが存在しない場合はNoneを返す（従来どおりの契約を維持）。
    """
    if not filename:
        return None
    p = _resolve_within_dir(SFX_DIR, filename)
    return p if p.exists() else None


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def unrendered_enabled_shot_ids(plan):
    """有効(enabled)かつclip_path未生成（None/空）のショットidを順序どおり返す。

    render開始前のガード（POST /api/projects/{id}/render）専用の軽量チェック。
    validate_plan()とは別関数にしているのは、PUT /plan（下書き保存）では
    未生成ショットの保存自体は許可しつつ、render開始だけは拒否したいため
    （＝許容範囲がエンドポイントによって異なる）。
    """
    if not isinstance(plan, dict):
        return []
    shots = plan.get("shots")
    if not isinstance(shots, list):
        return []
    ids = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        if not shot.get("enabled", True):
            continue
        if not shot.get("clip_path"):
            sid = shot.get("id")
            if sid:
                ids.append(sid)
    return ids


def validate_plan(project_id, plan, ng_words=None, ng_patterns=None):
    """plan（Studio形式）を検証する。Returns (ok:bool, errors:list[str], normalized:dict|None)。

    検証内容（KR4準拠）:
      - shots: id重複なし/order整数/enabled bool/clip_path実在/
        trim範囲 0<=start<end<=source_duration
      - bgm.file / sfx[].file が assets 配下に実在する
      - subtitle_style の値域
      - narration_text/shots captionのコンプライアンスNGワード検査（compliance.py流用）

    ng_patterns: 商品アフィリエイト動画モード時のみ呼び出し側が
    compliance.BEAUTY_YAKKIHO_NG_PATTERNS を渡す（通常モードはNoneのまま=従来どおり不変）。
    """
    errors = []
    if not isinstance(plan, dict):
        return False, ["plan はオブジェクト(dict)である必要があります"], None

    shots_raw = plan.get("shots")
    normalized_shots = []
    if not isinstance(shots_raw, list):
        errors.append("plan.shots はリストである必要があります")
        shots_raw = []

    seen_ids = set()
    for i, shot in enumerate(shots_raw):
        if not isinstance(shot, dict):
            errors.append("plan.shots[{}] はオブジェクトである必要があります".format(i))
            continue
        sid = shot.get("id")
        if not sid or not isinstance(sid, str):
            errors.append("plan.shots[{}].id は非空文字列である必要があります".format(i))
            continue
        if sid in seen_ids:
            errors.append("plan.shots の id が重複しています: {!r}".format(sid))
            continue

        order = shot.get("order", i)
        if not isinstance(order, int):
            errors.append("plan.shots[{}(id={})].order は整数である必要があります".format(i, sid))
            continue
        enabled = shot.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append("plan.shots[{}(id={})].enabled は真偽値である必要があります".format(i, sid))
            continue

        # clip_path は None/空文字を許容する（= まだビジュアル生成が完了していないショット。
        # BUG-10対応でjobs.pyはdirector企画をclip_path=Noneのまま先行保存する設計のため、
        # ここで非空を必須にするとサーバ自身が書いたデータをサーバ自身が拒否してしまう）。
        # 非空の場合のみ、参照ファイルの実在チェックを行う。
        clip_path = shot.get("clip_path") or None
        if clip_path is not None:
            if not isinstance(clip_path, str):
                errors.append("plan.shots[{}(id={})].clip_path は文字列またはnullである必要があります".format(i, sid))
                continue
            try:
                resolved_clip = resolve_clip_path(project_id, clip_path)
            except UnsafeMediaPathError as exc:
                errors.append(
                    "plan.shots[{}(id={})].clip_path が不正です: {}".format(i, sid, exc)
                )
                continue
            if not resolved_clip.exists():
                errors.append(
                    "plan.shots[{}(id={})].clip_path が参照するファイルが存在しません: {}".format(i, sid, clip_path)
                )
                continue

        source_duration = shot.get("source_duration")
        if not _is_number(source_duration) or source_duration <= 0:
            errors.append(
                "plan.shots[{}(id={})].source_duration は正の数値である必要があります (got: {!r})".format(
                    i, sid, source_duration
                )
            )
            continue

        trim = shot.get("trim")
        if not isinstance(trim, dict):
            errors.append("plan.shots[{}(id={})].trim はオブジェクトである必要があります".format(i, sid))
            continue
        start = trim.get("start")
        end = trim.get("end")
        if not _is_number(start) or not _is_number(end):
            errors.append("plan.shots[{}(id={})].trim.start/end は数値である必要があります".format(i, sid))
            continue
        if not (0 <= start < end <= source_duration + 1e-6):
            errors.append(
                "plan.shots[{}(id={})].trim範囲が不正です: 0<=start<end<=source_duration を満たしてください "
                "(start={}, end={}, source_duration={})".format(i, sid, start, end, source_duration)
            )
            continue

        seen_ids.add(sid)
        normalized_shot = {
            "id": sid,
            "order": order,
            "enabled": enabled,
            "prompt": shot.get("prompt", "") or "",
            "caption": shot.get("caption", "") or "",
            "clip_path": clip_path,
            "source_duration": float(source_duration),
            "trim": {"start": float(start), "end": float(end)},
        }
        # image_path（商品アフィリエイト動画モード）は任意パススルー: 文字列ならそのまま保持する。
        # 実在チェックはここでは行わない（ビジュアルバックエンド側=resume/render実行時に検査される。
        # mock_backendは未実在ならgradients生成にフォールバック、higgsfield_backendは
        # VisualBackendErrorを送出する設計のため、ここで弾くと通常モードの互換性を壊すリスクがある）。
        image_path = shot.get("image_path")
        if isinstance(image_path, str) and image_path:
            normalized_shot["image_path"] = image_path
        normalized_shots.append(normalized_shot)

    narration_text = plan.get("narration_text", "")
    if not isinstance(narration_text, str):
        errors.append("plan.narration_text は文字列である必要があります")
        narration_text = ""

    bgm = plan.get("bgm")
    normalized_bgm = None
    if bgm is not None:
        if not isinstance(bgm, dict):
            errors.append("plan.bgm はオブジェクトかnullである必要があります")
        else:
            bgm_file = bgm.get("file")
            if bgm_file:
                try:
                    bgm_resolved = resolve_bgm_path(bgm_file)
                except UnsafeMediaPathError as exc:
                    errors.append("plan.bgm.file が不正です: {}".format(exc))
                else:
                    if bgm_resolved is None:
                        errors.append("plan.bgm.file が assets/bgm 配下に見つかりません: {!r}".format(bgm_file))
            gain_db = bgm.get("gain_db", -14)
            if not _is_number(gain_db):
                errors.append("plan.bgm.gain_db は数値である必要があります")
                gain_db = -14
            ducking = bgm.get("ducking", True)
            if not isinstance(ducking, bool):
                errors.append("plan.bgm.ducking は真偽値である必要があります")
                ducking = True
            normalized_bgm = {"file": bgm_file, "gain_db": float(gain_db), "ducking": ducking}

    sfx_raw = plan.get("sfx", [])
    normalized_sfx = []
    if not isinstance(sfx_raw, list):
        errors.append("plan.sfx はリストである必要があります")
        sfx_raw = []
    for i, s in enumerate(sfx_raw):
        if not isinstance(s, dict):
            errors.append("plan.sfx[{}] はオブジェクトである必要があります".format(i))
            continue
        sfx_file = s.get("file")
        if not sfx_file:
            errors.append("plan.sfx[{}].file が assets/sfx 配下に見つかりません: {!r}".format(i, sfx_file))
            continue
        try:
            sfx_resolved = resolve_sfx_path(sfx_file)
        except UnsafeMediaPathError as exc:
            errors.append("plan.sfx[{}].file が不正です: {}".format(i, exc))
            continue
        if sfx_resolved is None:
            errors.append("plan.sfx[{}].file が assets/sfx 配下に見つかりません: {!r}".format(i, sfx_file))
            continue
        at_sec = s.get("at_sec", 0.0)
        if not _is_number(at_sec) or at_sec < 0:
            errors.append("plan.sfx[{}].at_sec は0以上の数値である必要があります".format(i))
            continue
        gain_db = s.get("gain_db", -8)
        if not _is_number(gain_db):
            errors.append("plan.sfx[{}].gain_db は数値である必要があります".format(i))
            continue
        normalized_sfx.append({"file": sfx_file, "at_sec": float(at_sec), "gain_db": float(gain_db)})

    subtitle_style = plan.get("subtitle_style") or {}
    normalized_style = dict(DEFAULT_SUBTITLE_STYLE)
    if not isinstance(subtitle_style, dict):
        errors.append("plan.subtitle_style はオブジェクトである必要があります")
    else:
        font_size = subtitle_style.get("font_size", normalized_style["font_size"])
        if not _is_number(font_size) or not (20 <= font_size <= 200):
            errors.append("plan.subtitle_style.font_size は20〜200の数値である必要があります")
        else:
            normalized_style["font_size"] = int(font_size)
        accent_color = subtitle_style.get("accent_color", normalized_style["accent_color"])
        if not isinstance(accent_color, str) or not re.match(r"^#[0-9A-Fa-f]{6}$", accent_color):
            errors.append("plan.subtitle_style.accent_color は '#RRGGBB' 形式である必要があります")
        else:
            normalized_style["accent_color"] = accent_color
        position = subtitle_style.get("position", normalized_style["position"])
        if position not in VALID_POSITIONS:
            errors.append("plan.subtitle_style.position は {} のいずれかである必要があります".format(VALID_POSITIONS))
        else:
            normalized_style["position"] = position
        preset = subtitle_style.get("preset", normalized_style["preset"])
        if preset not in VALID_SUBTITLE_PRESETS:
            errors.append("plan.subtitle_style.preset は {} のいずれかである必要があります".format(VALID_SUBTITLE_PRESETS))
        else:
            normalized_style["preset"] = preset

    # コンプライアンス検査（compliance.py流用）: 景表法NG表現・競合名義を弾く
    compat_plan = {
        "concept": "",
        "hook": "",
        "narration_script": narration_text,
        "shots": [
            {"id": s["id"], "caption_jp": s["caption"], "visual_prompt": s["prompt"]}
            for s in normalized_shots
        ],
    }
    check = compliance.check_plan(compat_plan, ng_words=ng_words, ng_patterns=ng_patterns)
    if not check["ok"]:
        for v in check["violations"]:
            errors.append(
                "コンプライアンス違反: フィールド={} NGワード={!r}".format(v["field"], v["word"])
            )

    if errors:
        return False, errors, None

    normalized_plan = {
        "shots": normalized_shots,
        "narration_text": narration_text.strip(),
        "bgm": normalized_bgm,
        "sfx": normalized_sfx,
        "subtitle_style": normalized_style,
    }
    return True, [], normalized_plan
