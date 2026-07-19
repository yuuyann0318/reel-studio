# -*- coding: utf-8 -*-
"""参考TikTok/Reels動画URL -> 文字起こし -> 台本仕様JSON(spec)化。

director.py の TTP（Try To Play/参考動画の構成再現）注入向けの入力を作る。
既定実装は yt-dlp（音声抽出）-> Fish Audio ASR（文字起こし）-> claude_runner.call_claude_json
（構成解析: ビート分割/リズム抽出）の3段。すべて DI（依存注入）で差し替え可能にしてあり、
テストは実ネットワーク/実yt-dlp/実claude呼び出しを一切行わない。

pipeline/tts.py の FishAudioTTSBackend（APIキー未設定・認証エラー・リトライ全滅時は例外を
出さずフォールバック/エラー文字列を返す設計）と、pipeline/claude_runner.py の
subprocess.Popen + start_new_session=True + SIGTERM->5秒->SIGKILL（`_kill_tree`）による
タイムアウト処理パターンを踏襲する。

Python 3.9 互換構文のみ。stdlibのみ使用（外部依存追加は禁止）。
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from pipeline.claude_runner import _kill_tree, call_claude_json
except Exception:  # pragma: no cover - claude CLI周りが未整備でもimportを壊さない
    call_claude_json = None

    def _kill_tree(proc):  # type: ignore[no-redef]
        """claude_runner未整備環境向けの最小フォールバック(SIGTERMのみ)。"""
        try:
            proc.terminate()
        except Exception:
            pass

from pipeline.config import project_root

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")
_REFERENCE_ANALYSIS_PROMPT_FILE = "reference_analysis_prompt.txt"

DEFAULT_DOWNLOAD_TIMEOUT_SEC = 120
DEFAULT_MAX_VIDEO_SEC = 180

_FISH_ASR_URL = "https://api.fish.audio/v1/asr"
_FISH_ASR_DEFAULT_API_KEY_ENV = "FISH_AUDIO_API_KEY"
_FISH_ASR_RETRY_BACKOFF_SEC = [2, 5]
_FISH_ASR_MAX_RETRIES = 2
_FISH_ASR_DEFAULT_TIMEOUT_SEC = 60

_VALID_BEAT_ROLES = ("hook", "develop", "proof", "cta")
_REQUIRED_RHYTHM_KEYS = ("sentence_count", "avg_sentence_len", "max_sentence_len", "tone", "endings")
_MIN_TRANSCRIPT_LEN = 30

_OVERLAP_STRIP_PATTERN = re.compile(
    r"[\s　。、,.!?！？「」『』・…\-—―~〜\"'’‘”“()（）\[\]{}:：;；#＃*＊]+"
)


def _load_prompt(name):
    path = os.path.join(_PROMPT_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _safe_remove(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


_TMP_DIR_CLEANUP_PREFIX = "href_reference_"


def _is_cleanable_reference_tmp_dir(tmp_dir):
    """shutil.rmtreeで安全に削除してよい一時ディレクトリかを判定する。

    fetch_audio はDI(依存注入)で差し替え可能なため、テストや将来のカスタム実装が
    tmp_dir に共有ディレクトリ・プロジェクトルート・"."等を誤って返す可能性がある。
    default_fetch_audio が tempfile.mkdtemp(prefix="href_reference_") で作った、
    このモジュールが完全所有する一時ディレクトリだけを削除対象として認める
    （命名規則を満たさないパスは呼び出し元が管理する領域とみなし削除しない）。
    """
    if not tmp_dir or not isinstance(tmp_dir, str):
        return False
    try:
        base = os.path.basename(os.path.normpath(tmp_dir))
    except Exception:
        return False
    return base.startswith(_TMP_DIR_CLEANUP_PREFIX)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# URL正規化 / キャッシュ
# ---------------------------------------------------------------------------

def normalize_url(url):
    """URLからクエリ文字列・フラグメントを除去して正規化する。"""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return url.strip()
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _cache_dir(cfg=None):
    """キャッシュディレクトリを解決する。

    override が相対パスの場合は project_root() 基準で解決する(bin/ingest_reference.py の
    `project_root() / cache_dir` と同じ規律。CWD依存を避けるため)。override が絶対パスの場合は
    pathlib の仕様によりそのまま採用される。

    既知の残課題(今回スコープ外): このキャッシュディレクトリにはURLごとのspec JSONが
    無期限に蓄積される。上限/TTLによる自動削除は未実装。
    """
    cfg = cfg or {}
    override = (cfg.get("reference") or {}).get("cache_dir")
    if override:
        return str(project_root() / override)
    return str(project_root() / "assets" / "reference_cache")


def _cache_path_for(normalized_url, cfg=None):
    digest = hashlib.sha1((normalized_url or "").encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(cfg), "{}.json".format(digest))


def _load_cache(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(path, spec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 既定fetch_audio: yt-dlpで音声抽出 -> ffprobeで尺検査
# ---------------------------------------------------------------------------

def _probe_duration(ffprobe_bin, path):
    cmd = [
        ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except Exception:
        return 0.0
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


_URL_SCHEME_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


def default_fetch_audio(url, cfg=None):
    """yt-dlpで音声(m4a)を一時ディレクトリへ抽出する。

    Returns: {"path": str, "duration_sec": float, "tmp_dir": str}
    失敗時は例外(RuntimeError等)を送出する（呼び出し元のanalyze_referenceで捕捉する）。
    どちらのパスでも一時ディレクトリを残さない(成功時はtmp_dirを返し呼び出し元が掃除、
    失敗時はここでshutil.rmtreeする)。
    """
    if not isinstance(url, str) or not _URL_SCHEME_PATTERN.match(url.strip()):
        raise RuntimeError("参考動画URLはhttp(s)スキームである必要があります(got: {!r})".format(url))

    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    yt_dlp_bin = ref_cfg.get("yt_dlp_bin") or "yt-dlp"
    timeout_sec = ref_cfg.get("download_timeout_sec", DEFAULT_DOWNLOAD_TIMEOUT_SEC)
    max_video_sec = ref_cfg.get("max_video_sec", DEFAULT_MAX_VIDEO_SEC)
    ffprobe_bin = cfg.get("ffprobe_bin") or "ffprobe"

    tmp_dir = tempfile.mkdtemp(prefix="href_reference_")
    try:
        out_template = os.path.join(tmp_dir, "audio.%(ext)s")
        cmd = [yt_dlp_bin, "-x", "--audio-format", "m4a", "-o", out_template]
        # 音声抽出(-x)はffmpegが必要。システムPATHに無い環境(本リポジトリは同梱./bin/ffmpeg)でも
        # 動くよう、cfgのffmpeg_binの場所を--ffmpeg-locationで明示する(実機E2Eで発覚した必須指定)。
        # このオプションは必ず"--"より前に置く("--"の後はyt-dlp側でオプション解釈されず、
        # --ffmpeg-locationとその引数が誤って追加のダウンロード対象URLとして扱われてしまうため)。
        ffmpeg_bin = cfg.get("ffmpeg_bin")
        if ffmpeg_bin and os.path.sep in str(ffmpeg_bin):
            cmd += ["--ffmpeg-location", os.path.dirname(os.path.abspath(str(ffmpeg_bin)))]
        # "--" でオプション解釈を打ち切ってからurlを渡す(url文字列が"-o..."等のオプション風でも
        # yt-dlp側の引数として誤解釈されないようにするための防御)。オプションは全て上で追加済みの
        # ため、"--"の後にはurlのみを置く。
        cmd += ["--", url]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
        try:
            _stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise RuntimeError("yt-dlp実行がタイムアウトしました({}秒超過)".format(timeout_sec))

        if proc.returncode != 0:
            raise RuntimeError(
                "yt-dlp実行に失敗しました(exit {}): {}".format(proc.returncode, (stderr or "")[:300])
            )

        audio_path = os.path.join(tmp_dir, "audio.m4a")
        if not os.path.exists(audio_path):
            candidates = sorted(glob.glob(os.path.join(tmp_dir, "audio.*")))
            if not candidates:
                raise RuntimeError("yt-dlpの出力音声ファイルが見つかりません")
            audio_path = candidates[0]

        duration = _probe_duration(ffprobe_bin, audio_path)
        if duration and duration > max_video_sec:
            raise RuntimeError(
                "動画の尺が上限({:.0f}秒)を超えています(検出: 約{:.0f}秒)".format(max_video_sec, duration)
            )
        return {"path": audio_path, "duration_sec": duration, "tmp_dir": tmp_dir}
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# 既定asr_post: Fish Audio ASR（multipart POST・stdlibのみ）
# ---------------------------------------------------------------------------

def _encode_multipart(fields, file_field, file_name, file_bytes, file_content_type):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (fields or {}).items():
        parts.append("--{}".format(boundary).encode("utf-8"))
        parts.append('Content-Disposition: form-data; name="{}"'.format(key).encode("utf-8"))
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    parts.append("--{}".format(boundary).encode("utf-8"))
    parts.append(
        'Content-Disposition: form-data; name="{}"; filename="{}"'.format(file_field, file_name).encode("utf-8")
    )
    parts.append("Content-Type: {}".format(file_content_type).encode("utf-8"))
    parts.append(b"")
    parts.append(file_bytes)
    parts.append("--{}--".format(boundary).encode("utf-8"))
    parts.append(b"")
    body = b"\r\n".join(parts)
    content_type = "multipart/form-data; boundary={}".format(boundary)
    return content_type, body


def _default_fish_asr_http_post(url, audio_path, api_key, timeout_sec=60):
    """Fish Audio ASR APIへmultipart POSTし、応答バイト列を返す。

    HTTPエラーは urllib.error.HTTPError、接続/タイムアウト等は OSError としてそのまま
    呼び出し元へ伝播させる（tts.pyの_default_fish_audio_http_postと同じ規律）。
    """
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    fields = {"language": "ja", "ignore_timestamps": "false"}
    content_type, body = _encode_multipart(
        fields, "audio", os.path.basename(audio_path) or "audio.m4a", audio_bytes, "audio/m4a"
    )
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer {}".format(api_key))
    req.add_header("Content-Type", content_type)
    resp = urllib.request.urlopen(req, timeout=timeout_sec)
    try:
        return resp.read()
    finally:
        resp.close()


def default_asr_post(audio_path, cfg=None, _http_post=None, _sleep=None):
    """Fish Audio ASRで文字起こしする（非raise。例外を出さず結果dictで返す）。

    Returns: {"ok": bool, "text": str, "duration": float, "segments": list, "error": str|None}
    - 401/403/400 -> 即失敗
    - 402 -> 「Fish Audioの残高不足です」で即失敗
    - 5xx/接続断 -> バックオフ([2,5]秒)で最大2回リトライ後、失敗
    """
    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    api_key_env = ref_cfg.get("api_key_env") or _FISH_ASR_DEFAULT_API_KEY_ENV
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return {
            "ok": False, "text": "", "duration": 0.0, "segments": [],
            "error": "Fish AudioのAPIキーが未設定です({}が空です)".format(api_key_env),
        }

    http_post = _http_post or _default_fish_asr_http_post
    sleep = _sleep or time.sleep
    timeout_sec = ref_cfg.get("asr_timeout_sec", _FISH_ASR_DEFAULT_TIMEOUT_SEC)

    raw = None
    attempt = 0
    while True:
        try:
            raw = http_post(_FISH_ASR_URL, audio_path, api_key, timeout_sec=timeout_sec)
            break
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 402:
                return {"ok": False, "text": "", "duration": 0.0, "segments": [], "error": "Fish Audioの残高不足です"}
            if code in (400, 401, 403):
                return {
                    "ok": False, "text": "", "duration": 0.0, "segments": [],
                    "error": "Fish Audio ASRの認証/リクエストエラーです(HTTP {})".format(code),
                }
            if code >= 500:
                if attempt >= _FISH_ASR_MAX_RETRIES:
                    return {
                        "ok": False, "text": "", "duration": 0.0, "segments": [],
                        "error": "Fish Audio ASRがリトライ上限({}回)に達しました(HTTP {})".format(
                            _FISH_ASR_MAX_RETRIES, code
                        ),
                    }
                sleep(_FISH_ASR_RETRY_BACKOFF_SEC[min(attempt, len(_FISH_ASR_RETRY_BACKOFF_SEC) - 1)])
                attempt += 1
                continue
            return {
                "ok": False, "text": "", "duration": 0.0, "segments": [],
                "error": "Fish Audio ASRエラーです(HTTP {})".format(code),
            }
        except OSError as exc:
            if attempt >= _FISH_ASR_MAX_RETRIES:
                return {
                    "ok": False, "text": "", "duration": 0.0, "segments": [],
                    "error": "Fish Audio ASRへの接続に失敗しました: {}".format(exc),
                }
            sleep(_FISH_ASR_RETRY_BACKOFF_SEC[min(attempt, len(_FISH_ASR_RETRY_BACKOFF_SEC) - 1)])
            attempt += 1
            continue
        except Exception as exc:
            return {
                "ok": False, "text": "", "duration": 0.0, "segments": [],
                "error": "Fish Audio ASRで予期しないエラーが発生しました: {}".format(exc),
            }

    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else (raw or ""))
    except Exception as exc:
        return {
            "ok": False, "text": "", "duration": 0.0, "segments": [],
            "error": "Fish Audio ASR応答のJSON解析に失敗しました: {}".format(exc),
        }

    return {
        "ok": True,
        "text": payload.get("text") or "",
        "duration": payload.get("duration") or 0.0,
        "segments": payload.get("segments") or [],
        "error": None,
    }


# ---------------------------------------------------------------------------
# 構成解析プロンプト
# ---------------------------------------------------------------------------

def build_reference_analysis_prompt(transcript, segments, duration_sec):
    template = _load_prompt(_REFERENCE_ANALYSIS_PROMPT_FILE)
    segments_json = json.dumps(segments or [], ensure_ascii=False)
    return (
        template.replace("{TRANSCRIPT}", transcript or "")
        .replace("{SEGMENTS_JSON}", segments_json)
        .replace("{DURATION}", "{:.1f}".format(duration_sec or 0.0))
    )


def validate_reference_spec(data, transcript, segments, duration_sec, url):
    """claude構成解析の応答(data)を検査し、(ok, errors, normalized_spec)を返す。"""
    errors = []

    if not isinstance(data, dict):
        return False, ["解析結果はオブジェクト(dict)である必要があります"], None

    if not isinstance(transcript, str) or len(transcript.strip()) < _MIN_TRANSCRIPT_LEN:
        errors.append("transcript は{}字以上の非空文字列である必要があります".format(_MIN_TRANSCRIPT_LEN))

    beats_raw = data.get("beats")
    normalized_beats = []
    if not isinstance(beats_raw, list) or not beats_raw:
        errors.append("beats は非空リストである必要があります")
    else:
        for i, beat in enumerate(beats_raw):
            if not isinstance(beat, dict):
                errors.append("beats[{}] はオブジェクトではありません".format(i))
                continue
            role = beat.get("role")
            start = beat.get("start")
            end = beat.get("end")
            text = beat.get("text")
            summary = beat.get("summary")

            ok_item = True
            if role not in _VALID_BEAT_ROLES:
                errors.append(
                    "beats[{}].role が不正です(got: {!r}, expected one of {})".format(i, role, _VALID_BEAT_ROLES)
                )
                ok_item = False
            if not _is_number(start) or not _is_number(end) or float(end) < float(start):
                errors.append("beats[{}] の start/end が不正です(got start={!r}, end={!r})".format(i, start, end))
                ok_item = False
            if not isinstance(text, str) or not text.strip():
                errors.append("beats[{}].text は非空文字列である必要があります".format(i))
                ok_item = False

            if ok_item:
                normalized_beats.append(
                    {
                        "role": role,
                        "start": float(start),
                        "end": float(end),
                        "text": text.strip(),
                        "summary": summary.strip() if isinstance(summary, str) else "",
                    }
                )

    rhythm_raw = data.get("rhythm")
    normalized_rhythm = None
    if not isinstance(rhythm_raw, dict):
        errors.append("rhythm が存在しないかオブジェクトではありません")
    else:
        missing = [k for k in _REQUIRED_RHYTHM_KEYS if k not in rhythm_raw]
        if missing:
            errors.append("rhythm に必須キーが不足しています: {}".format(missing))
        elif not isinstance(rhythm_raw.get("endings"), list):
            errors.append("rhythm.endings はリストである必要があります")
        else:
            normalized_rhythm = {
                "sentence_count": rhythm_raw.get("sentence_count"),
                "avg_sentence_len": rhythm_raw.get("avg_sentence_len"),
                "max_sentence_len": rhythm_raw.get("max_sentence_len"),
                "tone": rhythm_raw.get("tone"),
                "endings": rhythm_raw.get("endings"),
            }

    if errors:
        return False, errors, None

    spec = {
        "version": 1,
        "url": url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "fish_asr",
        "duration_sec": float(duration_sec or 0.0),
        "transcript": transcript.strip(),
        "segments": segments or [],
        "beats": normalized_beats,
        "rhythm": normalized_rhythm,
        "telops": None,
        "cuts": None,
        "warnings": [],
    }
    return True, [], spec


# ---------------------------------------------------------------------------
# メイン: analyze_reference
# ---------------------------------------------------------------------------

def analyze_reference(url, cfg=None, progress_cb=None, fetch_audio=None, asr_post=None, claude_call=None):
    """参考動画URLを解析し、TTP注入用のspecを返す（例外を投げない）。

    Returns:
        {"ok": bool, "spec": dict|None, "source": "cache"|"fish_asr"|None,
         "cached": bool, "warnings": [str,...], "error": str|None}
    """
    cfg = cfg or {}
    warnings = []

    def _progress(stage, detail=None):
        if progress_cb is None:
            return
        try:
            progress_cb(stage, detail)
        except Exception:
            pass

    normalized = normalize_url(url)
    if not normalized:
        return {
            "ok": False, "spec": None, "source": None, "cached": False,
            "warnings": warnings, "error": "参考動画のURLが不正です",
        }

    cache_path = _cache_path_for(normalized, cfg)
    _progress("cache_check")
    cached_spec = _load_cache(cache_path)
    if cached_spec is not None:
        return {"ok": True, "spec": cached_spec, "source": "cache", "cached": True, "warnings": [], "error": None}

    fetch_audio_fn = fetch_audio or default_fetch_audio
    asr_post_fn = asr_post or default_asr_post
    claude_call_fn = claude_call if claude_call is not None else call_claude_json

    _progress("download")
    try:
        audio_info = fetch_audio_fn(normalized, cfg)
    except Exception as exc:
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画の音声取得に失敗しました: {}".format(exc),
        }

    audio_path = (audio_info or {}).get("path")
    duration_sec = (audio_info or {}).get("duration_sec") or 0.0
    tmp_dir = (audio_info or {}).get("tmp_dir")
    cleanable_tmp_dir = tmp_dir if _is_cleanable_reference_tmp_dir(tmp_dir) else None
    if not audio_path:
        if cleanable_tmp_dir:
            shutil.rmtree(cleanable_tmp_dir, ignore_errors=True)
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画の音声取得に失敗しました: 音声ファイルが取得できませんでした",
        }

    _progress("asr")
    try:
        asr_result = asr_post_fn(audio_path, cfg)
    except Exception as exc:
        asr_result = {"ok": False, "error": "文字起こし処理で例外が発生しました: {}".format(exc)}
    finally:
        # fetch_audio_fn がこのモジュール所有の一時ディレクトリ(tmp_dir)を返した場合
        # (既定実装のdefault_fetch_audio)はディレクトリごと掃除する。カスタムのfetch_audio
        # (テスト等)が命名規則を満たさないtmp_dirを返した場合は、呼び出し元が管理する領域を
        # 誤って広く削除しないよう、ファイル単体のみ削除する(_is_cleanable_reference_tmp_dir)。
        if cleanable_tmp_dir:
            shutil.rmtree(cleanable_tmp_dir, ignore_errors=True)
        else:
            _safe_remove(audio_path)

    if not asr_result or not asr_result.get("ok"):
        err = (asr_result or {}).get("error") or "文字起こしに失敗しました"
        return {"ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings, "error": err}

    transcript = asr_result.get("text") or ""
    segments = asr_result.get("segments") or []
    if not duration_sec:
        duration_sec = asr_result.get("duration") or 0.0

    if claude_call_fn is None:
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画の構成解析(claude呼び出し)が利用できません",
        }

    _progress("analyze")
    prompt = build_reference_analysis_prompt(transcript, segments, duration_sec)
    timeout_sec = cfg.get("claude_timeout_sec", 600)
    try:
        result = claude_call_fn(prompt, timeout_sec=timeout_sec)
    except Exception as exc:
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画の構成解析に失敗しました: {}".format(exc),
        }

    if not result or not result.get("ok") or not isinstance(result.get("data"), dict):
        err = (result or {}).get("error") or "参考動画の構成解析の応答が取得できませんでした"
        return {"ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings, "error": err}

    ok, errors, spec = validate_reference_spec(result["data"], transcript, segments, duration_sec, normalized)
    if not ok:
        return {
            "ok": False, "spec": None, "source": None, "cached": False,
            "warnings": warnings + errors, "error": "参考動画の解析結果の検証に失敗しました",
        }

    _write_cache(cache_path, spec)
    _progress("done")
    return {"ok": True, "spec": spec, "source": "fish_asr", "cached": False, "warnings": warnings, "error": None}


# ---------------------------------------------------------------------------
# 丸写し検出
# ---------------------------------------------------------------------------

def _normalize_for_overlap(text):
    # NFKC正規化を先に行い、全角/半角の表記ゆれ(数字・英字・記号)で丸写し検出が
    # すり抜けないようにする(例: 全角"１５字"と半角"15字")。
    normalized = unicodedata.normalize("NFKC", text or "")
    return _OVERLAP_STRIP_PATTERN.sub("", normalized)


def find_verbatim_overlap(reference_text, script_text, min_len=15):
    """reference_text と script_text の間で、正規化後min_len字以上連続一致する箇所を検出する。

    正規化: 空白/句読点/記号を除去してから比較する（表記ゆれによる過検知/見逃しを防ぐ）。
    Returns: list[str]（一致した部分文字列。無ければ空リスト）
    """
    ref_norm = _normalize_for_overlap(reference_text)
    script_norm = _normalize_for_overlap(script_text)
    if not ref_norm or not script_norm:
        return []

    matcher = difflib.SequenceMatcher(None, ref_norm, script_norm, autojunk=False)
    overlaps = []
    for block in matcher.get_matching_blocks():
        if block.size >= min_len:
            overlaps.append(script_norm[block.b: block.b + block.size])
    return overlaps
