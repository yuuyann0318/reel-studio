# -*- coding: utf-8 -*-
"""BGMライブラリ管理: inbox取り込み・ムード判定・シード決定論選曲・直近回避。

役割:
  - assets/bgm/inbox/ にユーザーが置いた音源(mp3/wav/m4a)を検出し、
    loudnormで-18 LUFSに正規化しつつ assets/bgm/library/<slug>.m4a へ変換、
    manifest.json に追記する（重複sha1はスキップ、原本は inbox/imported/ へ退避）。
  - manifest から mood にマッチする曲をシード付きで乱択する pick_bgm()。
    直近使用2曲を回避することで「BGMが毎回同じ」を防ぐ。

DESIGN:
  - 「先頭トークン=既知ムード」規約でファイル名からムードを推定
    （例: "upbeat_夏っぽい.mp3" → upbeat）。区切りは _ / - / 半角スペース。
    未知トークンは "any"（どの mood 要求でも fallback 候補になる）。
  - history は assets/bgm/.history.json に「recent: [{project_id,file,mood,ts}]」の
    ロールバック用リングバッファ。直近2件のfileを除外候補から外す。
  - manifest entry の file は BGM_DIR 相対（例: "library/upbeat_bright_01.m4a"）を
    受理（studio/server/projects.resolve_bgm_path が BGM_DIR 相対を解決可能）。

Python 3.9 互換のみ。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import fcntl  # POSIX のみ（macOS/Linuxで有効）
except Exception:  # pragma: no cover - Windows等
    fcntl = None

from pipeline.config import project_root

_BGM_ROOT = project_root() / "assets" / "bgm"
_INBOX_DIR = _BGM_ROOT / "inbox"
_IMPORTED_DIR = _INBOX_DIR / "imported"
_LIBRARY_DIR = _BGM_ROOT / "library"
_MANIFEST_PATH = _BGM_ROOT / "manifest.json"
_HISTORY_PATH = _BGM_ROOT / ".history.json"

# 既知ムード集合（"any" はムード不明な曲がどの mood 要求でも fallback 候補になる）
KNOWN_MOODS = {"upbeat", "calm", "emotional", "dramatic", "lofi", "any"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a"}
_HISTORY_MAX = 64
_AVOID_LAST_N = 2


# ---------------------------------------------------------------------------
# パス/入出力ユーティリティ
# ---------------------------------------------------------------------------

def bgm_root():
    return _BGM_ROOT


def inbox_dir():
    return _INBOX_DIR


def imported_dir():
    return _IMPORTED_DIR


def library_dir():
    return _LIBRARY_DIR


def manifest_path():
    return _MANIFEST_PATH


def history_path():
    return _HISTORY_PATH


def _load_manifest_from(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_manifest():
    return _load_manifest_from(_MANIFEST_PATH)


def _write_manifest_to(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _slugify_stem(stem, max_len=48):
    s = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー_-]+", "-", stem).strip("-_") or "track"
    return s[:max_len]


def mood_from_filename(stem):
    """ファイル名の先頭トークンから既知ムードを推定。未知は "any"。"""
    token = re.split(r"[_\-\s]+", stem, maxsplit=1)[0].lower()
    if token in KNOWN_MOODS:
        return token
    return "any"


# ---------------------------------------------------------------------------
# デフォルト prober / converter（テストではモック差し替え可）
# ---------------------------------------------------------------------------

def _default_prober(cfg):
    ffprobe_bin = (cfg or {}).get("ffprobe_bin") or str(project_root() / "bin" / "ffprobe")

    def _probe(path):
        try:
            r = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                check=True, capture_output=True, timeout=30,
            )
            return float(r.stdout.decode("utf-8").strip())
        except Exception:
            return None
    return _probe


def _default_converter(cfg):
    ffmpeg_bin = (cfg or {}).get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")

    def _convert(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_bin, "-y", "-i", str(src),
            "-af", "loudnorm=I=-18:TP=-2.0:LRA=7",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(dst),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return _convert


# ---------------------------------------------------------------------------
# inbox 取り込み
# ---------------------------------------------------------------------------

def import_inbox(cfg=None, prober=None, converter=None,
                 inbox_dir_override=None, library_dir_override=None,
                 imported_dir_override=None, manifest_path_override=None):
    """assets/bgm/inbox/ を走査して未取り込みの音源を library/ に変換配置し manifest 追記。

    引数:
      - cfg: config dict（ffmpeg/ffprobe binary path 取得用）
      - prober: (path) -> duration_sec or None（Noneでffprobe既定）
      - converter: (src, dst) -> None（Noneでffmpeg既定）
      - *_override: テスト用に各ディレクトリ/マニフェストを差し替える

    戻り値:
      {"imported": [entry, ...], "skipped": [{"file": ..., "reason": ...}, ...]}
    """
    ibx = Path(inbox_dir_override) if inbox_dir_override else _INBOX_DIR
    imp = Path(imported_dir_override) if imported_dir_override else _IMPORTED_DIR
    lib = Path(library_dir_override) if library_dir_override else _LIBRARY_DIR
    mfp = Path(manifest_path_override) if manifest_path_override else _MANIFEST_PATH

    if not ibx.exists():
        return {"imported": [], "skipped": []}
    imp.mkdir(parents=True, exist_ok=True)
    lib.mkdir(parents=True, exist_ok=True)

    prober = prober or _default_prober(cfg)
    converter = converter or _default_converter(cfg)

    manifest = _load_manifest_from(mfp)
    known_sha1 = {e.get("sha1") for e in manifest if isinstance(e, dict) and e.get("sha1")}
    result = {"imported": [], "skipped": []}

    for src in sorted(ibx.iterdir()):
        if src.is_dir():
            continue
        if src.suffix.lower() not in _AUDIO_EXTS:
            continue
        try:
            sha1 = _sha1_of(src)
        except OSError as exc:
            result["skipped"].append({"file": src.name, "reason": "read-error: {}".format(exc)})
            continue
        if sha1 in known_sha1:
            # 既取り込み → imported へ退避（重複を inbox に残さない）
            try:
                shutil.move(str(src), str(imp / src.name))
            except Exception:
                pass
            result["skipped"].append({"file": src.name, "reason": "duplicate-sha1"})
            continue

        mood = mood_from_filename(src.stem)
        duration = prober(src)
        if duration is None or duration <= 0.0:
            result["skipped"].append({"file": src.name, "reason": "probe-failed"})
            continue

        slug = _slugify_stem(src.stem)
        base = slug
        idx = 1
        dst = lib / (base + ".m4a")
        while dst.exists():
            idx += 1
            dst = lib / ("{}-{}.m4a".format(base, idx))

        try:
            converter(src, dst)
        except Exception as exc:
            result["skipped"].append({"file": src.name, "reason": "convert-failed: {}".format(exc)})
            continue

        entry = {
            "file": "library/" + dst.name,
            "mood": mood,
            "sha1": sha1,
            "duration_sec": round(float(duration), 3),
            "license": "user-provided (inbox import)",
            "loop_ok": True,
        }
        manifest.append(entry)
        known_sha1.add(sha1)
        result["imported"].append(entry)

        # 原本を imported/ へ移動（同名衝突時はサフィックス付与）
        target = imp / src.name
        tgt_idx = 1
        while target.exists():
            tgt_idx += 1
            target = imp / ("{}-{}{}".format(src.stem, tgt_idx, src.suffix))
        try:
            shutil.move(str(src), str(target))
        except Exception:
            pass

    if result["imported"]:
        _write_manifest_to(mfp, manifest)
    return result


# ---------------------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------------------

def _load_history_from(path):
    if not path.exists():
        return {"recent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("recent"), list):
            return data
    except Exception:
        pass
    return {"recent": []}


def load_history():
    return _load_history_from(_HISTORY_PATH)


def _save_history_to(path, history):
    """history JSON をアトミック書き込み（tempfile→os.replace）＋ロック排他で永続化する。

    - 同時に複数プロセス/スレッドが pick_bgm したときの history 破損を避けるため
      同じディレクトリの .lock ファイルに fcntl.flock(EX) を取ってから書く。
    - tempfile を同じディレクトリに作って os.replace することで、途中失敗しても
      既存ファイルは常に妥当な JSON のまま残る（部分書き込みが観測されない）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(history, ensure_ascii=False, indent=2) + "\n"

    lock_path = path.parent / (path.name + ".lock")
    lock_fp = None
    if fcntl is not None:
        try:
            lock_fp = open(lock_path, "a+")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            # ロック取得失敗はブロックしない（ベストエフォート）
            if lock_fp is not None:
                try:
                    lock_fp.close()
                except Exception:
                    pass
                lock_fp = None

    try:
        # 同一ディレクトリで tempfile→rename（os.replace）で原子的に置換
        fd, tmp_name = tempfile.mkstemp(
            prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_name, str(path))
        except Exception:
            # 失敗時は tempfile を掃除して再送出
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            raise
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fp.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 選曲
# ---------------------------------------------------------------------------

def pick_bgm(mood, seed=None, manifest=None, history=None,
             record_project_id=None, history_path_override=None):
    """ムード適合曲からシード乱択＋直近2曲を回避して1曲を選ぶ。

    引数:
      - mood: "upbeat"/"calm"/"emotional"/"dramatic"/"lofi"/None/"none"
      - seed: 乱数シード（Noneならtimeベース。決定論を要するテストは明示する）
      - manifest: 事前ロード済み manifest（Noneなら実ファイルから）
      - history: 事前ロード済み history（Noneなら実ファイルから）
      - record_project_id: 指定時は選曲結果を history に追記して永続化する
      - history_path_override: テスト用の history 書き出し先

    戻り値: manifest entry(dict) or None
    """
    if not mood or mood == "none":
        return None
    if manifest is None:
        manifest = load_manifest()
    if history is None:
        history = load_history()

    def _valid(e):
        return isinstance(e, dict) and e.get("file")

    def _is_legacy_placeholder(e):
        # 旧sine波プレースホルダ(upbeat_01.mp3等・manifestで legacy:true)は、
        # 本物寄りのライブラリ曲が1曲でもあれば選曲プールから除外する
        # (実機E2Eでプレースホルダが乱択され「毎回同じ安っぽい音」の一因になった)。
        return bool(e.get("legacy"))

    all_valid = [e for e in manifest if _valid(e)]
    non_legacy = [e for e in all_valid if not _is_legacy_placeholder(e)]
    pool_base = non_legacy if non_legacy else all_valid

    exact = [e for e in pool_base if e.get("mood") == mood]
    any_pool = [e for e in pool_base if e.get("mood") == "any"]
    fallback_all = pool_base

    if exact:
        candidates = exact + any_pool
    elif any_pool:
        candidates = any_pool
    elif fallback_all:
        # mood 完全ミスマッチ時は全体から選ぶ（BGMを完全に落とすよりベター）
        candidates = fallback_all
    else:
        return None

    # 直近使用の回避: ただし自プロジェクトの過去エントリは「他人ではなく自分の履歴」
    # なので回避対象から外す（再実行で同曲を維持するため。決定論と回避の衝突回避）
    recent_slice = history.get("recent", [])[-_AVOID_LAST_N:]
    recent_files = [
        r.get("file") for r in recent_slice
        if r.get("file")
        and not (record_project_id and r.get("project_id") == record_project_id)
    ]
    filtered = [e for e in candidates if e.get("file") not in recent_files]
    pool = filtered if filtered else candidates

    # 安定順序でソートしてから乱択（辞書入力順に依存しない決定論）
    pool_sorted = sorted(pool, key=lambda e: e.get("file", ""))
    rng = random.Random(seed)
    chosen = rng.choice(pool_sorted)

    if record_project_id:
        recent_list = history.setdefault("recent", [])
        # 同じ (project_id, mood) のエントリが既に recent にあれば append しない
        # （同じプロジェクトを再実行したときの重複履歴を防ぐ。決定論選曲なので
        #  同じ曲が選ばれても回避対象が増えて次回選択がブレる副作用を避ける）
        already_recorded = any(
            isinstance(r, dict)
            and r.get("project_id") == record_project_id
            and r.get("mood") == chosen.get("mood")
            for r in recent_list
        )
        if not already_recorded:
            recent_list.append({
                "project_id": record_project_id,
                "file": chosen.get("file"),
                "mood": chosen.get("mood"),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            history["recent"] = recent_list[-_HISTORY_MAX:]
            _save_history_to(
                Path(history_path_override) if history_path_override else _HISTORY_PATH,
                history,
            )

    return chosen
