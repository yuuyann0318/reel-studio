# -*- coding: utf-8 -*-
"""参考動画URL -> フルDL -> カット検出 + テロップOCR + SEイベント検出 -> 統合 spec v2。

pipeline/reference.py (音声のみ解析 v1) の土台に映像込みの多モーダル解析を追加する v2。
公開関数は analyze_reference_v2()。DIで全ての外部呼び出しを差し替え可能にしてある。

- yt-dlp で映像込み(bv*+ba/b, mp4)DL → 同梱 bin/ffmpeg で音声を m4a 分離
- 同梱 bin/ffmpeg + select=gt(scene,THRESH),showinfo でカット検出
- 各カット区間から2枚(直後+0.2s / 区間中央)を抽出し、claude vision で1回にバッチしてOCR/映像記述
- 同梱 bin/ffmpeg + astats/ametadata で HF(>=2kHz) と全帯域の RMS 時系列を取り、+6dB 以上のジャンプを
  オンセットとして分類 (transition / riser / impact / pop / shimmer / other)
- 統合プロンプトで claude(テキスト) にビート・カット・テロップ・SEを融合させ v2 spec を得る

Python 3.9 互換構文のみ。stdlibのみ使用（外部依存追加は禁止）。中間素材(mp4/png/m4a)は
解析後に必ず削除する（GDPR/著作物として保持しない方針・タスク要件）。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from pipeline.claude_runner import (
        _kill_tree,
        call_claude_json,
        call_claude_vision_json,
    )
except Exception:  # pragma: no cover - CLI周りが未整備でもimportを壊さない
    call_claude_json = None
    call_claude_vision_json = None

    def _kill_tree(proc):  # type: ignore[no-redef]
        try:
            proc.terminate()
        except Exception:
            pass

from pipeline.config import load_config, project_root
from pipeline import reference as ref_v1  # v1 のフェッチ・ASR実装を再利用


_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")
_VISION_PROMPT_FILE = "reference_vision_prompt.txt"
_FUSION_PROMPT_FILE = "reference_fusion_prompt.txt"

_TMP_DIR_CLEANUP_PREFIX_V2 = "href_reference_v2_"

# ffmpeg showinfo は "pts_time:1.234" を stderr に流す
_SHOWINFO_PTS_RE = re.compile(r"pts_time:\s*([0-9]+\.?[0-9]*)")
# ffmpeg ametadata print はフレームごとに 2 行:
#   frame:0    pts:0       pts_time:0
#   lavfi.astats.Overall.RMS_level=-40.55
_AMETADATA_FRAME_RE = re.compile(r"frame:\s*\d+\s+pts:\s*\-?\d+\s+pts_time:\s*(\-?[0-9]+\.?[0-9]*)")
_AMETADATA_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level\s*=\s*(\-?[0-9]+\.?[0-9]*|nan|inf|\-inf)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# キャッシュ (v1 と衝突しないキー "_v2" サフィックス)
# ---------------------------------------------------------------------------

def _cache_dir(cfg=None):
    cfg = cfg or {}
    override = (cfg.get("reference") or {}).get("cache_dir")
    if override:
        return str(project_root() / override)
    return str(project_root() / "assets" / "reference_cache")


def _cache_path_for_v2(normalized_url: str, cfg=None) -> str:
    digest = hashlib.sha1((normalized_url or "").encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(cfg), "{}_v2.json".format(digest))


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
# 映像込みDL + 音声分離
# ---------------------------------------------------------------------------

def default_fetch_video(url, cfg=None) -> Dict[str, Any]:
    """yt-dlp で映像込み(mp4)をDLし、同梱ffmpegで音声(m4a)を分離する。

    Returns: {"video_path", "audio_path", "duration_sec", "tmp_dir"}
    失敗時は例外を投げる（tmp_dir は掃除して伝播）。
    """
    if not isinstance(url, str) or not ref_v1._URL_SCHEME_PATTERN.match(url.strip()):
        raise RuntimeError("参考動画URLはhttp(s)スキームである必要があります(got: {!r})".format(url))

    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    yt_dlp_bin_raw = ref_cfg.get("yt_dlp_bin") or "yt-dlp"
    # 相対パス指定は project_root() 基準で解決する(CWD 依存を避ける)。
    # 絶対パス・PATH解決名(例 "yt-dlp")はそのまま採用。
    if yt_dlp_bin_raw and os.path.sep in yt_dlp_bin_raw and not os.path.isabs(yt_dlp_bin_raw):
        yt_dlp_bin = str(project_root() / yt_dlp_bin_raw)
    else:
        yt_dlp_bin = yt_dlp_bin_raw
    timeout_sec = ref_cfg.get("download_timeout_sec", ref_v1.DEFAULT_DOWNLOAD_TIMEOUT_SEC)
    max_video_sec = ref_cfg.get("max_video_sec", ref_v1.DEFAULT_MAX_VIDEO_SEC)
    ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
    ffprobe_bin = cfg.get("ffprobe_bin") or str(project_root() / "bin" / "ffprobe")

    tmp_dir = tempfile.mkdtemp(prefix=_TMP_DIR_CLEANUP_PREFIX_V2)
    try:
        out_template = os.path.join(tmp_dir, "video.%(ext)s")
        cmd = [
            yt_dlp_bin,
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", out_template,
        ]
        if ffmpeg_bin and os.path.sep in str(ffmpeg_bin):
            cmd += ["--ffmpeg-location", os.path.dirname(os.path.abspath(str(ffmpeg_bin)))]
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
                "yt-dlp(v2)実行に失敗しました(exit {}): {}".format(proc.returncode, (stderr or "")[:300])
            )

        video_path = os.path.join(tmp_dir, "video.mp4")
        if not os.path.exists(video_path):
            candidates = sorted(glob.glob(os.path.join(tmp_dir, "video.*")))
            if not candidates:
                raise RuntimeError("yt-dlp(v2)の出力動画ファイルが見つかりません")
            video_path = candidates[0]

        duration = ref_v1._probe_duration(ffprobe_bin, video_path)
        if duration and duration > max_video_sec:
            raise RuntimeError(
                "動画の尺が上限({:.0f}秒)を超えています(検出: 約{:.0f}秒)".format(max_video_sec, duration)
            )

        # 音声分離: 可能なら stream copy (aac→m4a) で高速に。失敗時は AAC 再エンコード。
        audio_path = os.path.join(tmp_dir, "audio.m4a")
        try:
            _extract_audio(ffmpeg_bin, video_path, audio_path, copy=True)
        except Exception:
            _extract_audio(ffmpeg_bin, video_path, audio_path, copy=False)

        if not os.path.exists(audio_path):
            raise RuntimeError("音声分離に失敗しました(audio.m4aが生成されませんでした)")

        return {
            "video_path": video_path,
            "audio_path": audio_path,
            "duration_sec": duration or ref_v1._probe_duration(ffprobe_bin, audio_path) or 0.0,
            "tmp_dir": tmp_dir,
        }
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _extract_audio(ffmpeg_bin: str, video_path: str, audio_path: str, copy: bool = True) -> None:
    cmd = [str(ffmpeg_bin), "-y", "-i", video_path, "-vn"]
    if copy:
        cmd += ["-acodec", "copy"]
    else:
        cmd += ["-acodec", "aac", "-b:a", "128k"]
    cmd += [audio_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg audio extract failed(copy={}): {}".format(copy, (proc.stderr or b"").decode("utf-8", "replace")[:200])
        )


# ---------------------------------------------------------------------------
# カット検出
# ---------------------------------------------------------------------------

def parse_showinfo_stderr(stderr: str) -> List[float]:
    """ffmpeg select=gt(scene,X),showinfo の stderr から pts_time 秒列を抽出する。"""
    if not stderr:
        return []
    out = []
    for m in _SHOWINFO_PTS_RE.finditer(stderr):
        try:
            out.append(float(m.group(1)))
        except Exception:
            continue
    # 単調非減少にし、重複ほぼゼロを除去（0.02秒未満の重複は捨てる）
    out.sort()
    dedup = []
    for t in out:
        if not dedup or (t - dedup[-1]) > 0.02:
            dedup.append(t)
    return dedup


def detect_cuts_via_ffmpeg(ffmpeg_bin: str, video_path: str, threshold: float, timeout_sec: int = 120) -> List[float]:
    """ffmpeg を実行してカット秒列を返す（実実行。テストではDI差し替え）。"""
    thr = max(0.0, min(0.99, float(threshold)))
    cmd = [
        str(ffmpeg_bin), "-hide_banner", "-nostats", "-i", video_path,
        "-vf", "select='gt(scene,{:.3f})',showinfo".format(thr),
        "-fps_mode", "vfr", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
    return parse_showinfo_stderr((proc.stderr or b"").decode("utf-8", "replace"))


def synth_cuts_uniform(duration_sec: float, interval: float = 5.0) -> List[float]:
    """カット0件時の擬似カット(等分)。0秒は含めず interval ごとに刻む。"""
    if not duration_sec or duration_sec <= interval:
        return []
    out = []
    t = interval
    while t < duration_sec - 0.05:
        out.append(round(t, 3))
        t += interval
    return out


# ---------------------------------------------------------------------------
# フレーム抽出（カット区間から2枚。上限を超えたら等間引き）
# ---------------------------------------------------------------------------

def select_frame_times_from_cuts(cuts: List[float], duration_sec: float, max_frames: int = 40) -> List[float]:
    """各カット区間 (前カット, 次カット) から 2枚(直後+0.2s / 区間中央) の秒列を返す。

    上限 max_frames を超える場合は等間引きする。cuts は0秒を含まない前提（先頭区間は [0, cuts[0]]）。
    """
    boundaries = [0.0] + [float(c) for c in cuts] + [float(duration_sec or 0.0)]
    # 重複除去+単調非減少
    dedup = []
    for b in boundaries:
        if not dedup or b > dedup[-1] + 1e-3:
            dedup.append(b)
    times = []
    for i in range(len(dedup) - 1):
        seg_start = dedup[i]
        seg_end = dedup[i + 1]
        if seg_end - seg_start < 0.1:
            continue
        # 直後 +0.2s (区間長が0.4s未満なら 区間長の1/4)
        offset = 0.2 if (seg_end - seg_start) >= 0.4 else max(0.05, (seg_end - seg_start) / 4.0)
        t1 = seg_start + offset
        # 区間中央
        t2 = seg_start + (seg_end - seg_start) / 2.0
        times.append(round(min(t1, seg_end - 0.02), 3))
        if abs(t2 - t1) > 0.1:
            times.append(round(min(t2, seg_end - 0.02), 3))
    times = sorted(set(times))
    if len(times) <= max_frames:
        return times
    # 等間引き
    step = len(times) / float(max_frames)
    thinned = []
    for i in range(max_frames):
        idx = int(i * step)
        if idx < len(times):
            thinned.append(times[idx])
    return sorted(set(thinned))


def extract_frames_at_times(
    ffmpeg_bin: str,
    video_path: str,
    times: List[float],
    out_dir: str,
    timeout_sec: int = 90,
) -> List[Dict[str, Any]]:
    """指定秒に対応するフレームPNGを out_dir に生成し、[{index,time,path},...] を返す。"""
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        path = os.path.join(out_dir, "frame_{:03d}.png".format(i + 1))
        cmd = [
            str(ffmpeg_bin), "-y", "-ss", "{:.3f}".format(max(0.0, float(t))),
            "-i", video_path, "-frames:v", "1", "-q:v", "3", path,
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
            if proc.returncode == 0 and os.path.exists(path):
                frames.append({"index": i + 1, "time": float(t), "path": path})
        except Exception:
            continue
    return frames


# ---------------------------------------------------------------------------
# ametadata / astats パーサ・オンセット検出・分類
# ---------------------------------------------------------------------------

def parse_ametadata_stderr(stderr: str) -> List[Tuple[float, float]]:
    """ffmpeg ametadata の stderr(または stdout)から (time_sec, rms_db) の時系列を抽出する。

    ametadata print は現在のフレームの pts_time 行と、直後に "lavfi.astats.Overall.RMS_level=..." 行を
    連続して出す。inf/nan は -120dB として扱う（BOSS区間の除算防止）。
    """
    if not stderr:
        return []
    current_t: Optional[float] = None
    series: List[Tuple[float, float]] = []
    for line in stderr.splitlines():
        line = line.strip()
        mframe = _AMETADATA_FRAME_RE.search(line)
        if mframe:
            try:
                current_t = float(mframe.group(1))
            except Exception:
                current_t = None
            continue
        mrms = _AMETADATA_RMS_RE.search(line)
        if mrms and current_t is not None:
            raw = mrms.group(1).lower()
            if raw in ("nan", "-inf", "inf"):
                rms = -120.0
            else:
                try:
                    rms = float(raw)
                except Exception:
                    current_t = None
                    continue
            series.append((current_t, rms))
            current_t = None
    return series


def detect_onsets_from_rms_series(series: List[Tuple[float, float]], jump_db: float = 6.0) -> List[float]:
    """RMS時系列から +jump_db 以上のジャンプ点(オンセット秒)を返す。

    連続フレームで階段状に上がった場合は先頭のみ拾う（最小0.15秒の不応期）。
    """
    if not series:
        return []
    onsets: List[float] = []
    prev_rms = series[0][1]
    last_onset_t = -1.0
    for t, rms in series[1:]:
        if rms - prev_rms >= jump_db and (t - last_onset_t) >= 0.15:
            onsets.append(round(float(t), 3))
            last_onset_t = t
        prev_rms = rms
    return onsets


def _in_any_segment(t: float, segments: List[Dict[str, Any]], pad: float = 0.10) -> bool:
    for seg in segments or []:
        s = seg.get("start")
        e = seg.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            continue
        if (float(s) - pad) <= t <= (float(e) + pad):
            return True
    return False


def _nearest_cut(t: float, cuts: List[float]) -> Optional[float]:
    if not cuts:
        return None
    best = None
    best_d = 1e9
    for c in cuts:
        d = abs(float(c) - t)
        if d < best_d:
            best_d = d
            best = float(c)
    return best


def classify_onsets(
    cuts: List[float],
    speech_segments: List[Dict[str, Any]],
    hf_onsets: List[float],
    full_onsets: List[float],
) -> List[Dict[str, Any]]:
    """オンセットを transition / riser / impact / pop / shimmer / other に分類する。

    仕様(タスク文書より):
    - ASR発話区間内の "全帯域" オンセットは発話由来として除外
    - カット±0.15s → transition (アンカ=cut)
    - カット前 0.5〜1.5s の単調増加ジャンプ → riser (アンカ=cut)
    - HF帯単発 → pop / shimmer 候補 (アンカ=none)
    - 全帯域単発 → impact 候補 (アンカ=none)
    """
    events: List[Dict[str, Any]] = []
    seen_ts: Dict[Tuple[float, str], bool] = {}

    # 発話除外は全帯域側のみ（HF高域は発話のさ行以外は基本残す）
    full_filtered = [t for t in full_onsets if not _in_any_segment(t, speech_segments)]

    for t in full_filtered:
        near = _nearest_cut(t, cuts)
        if near is not None and abs(near - t) <= 0.15:
            _push_event(events, seen_ts, t, "transition", "cut", 0.9)
            continue
        # riser: カットより 0.5〜1.5s 手前
        riser_cut = next((c for c in cuts if 0.5 <= (c - t) <= 1.5), None)
        if riser_cut is not None:
            _push_event(events, seen_ts, t, "riser", "cut", 0.7)
            continue
        _push_event(events, seen_ts, t, "impact", "none", 0.5)

    for t in hf_onsets:
        # HFは発話でも残す（さ行の高域は正しい）が、既に transition/riser として登録済みのタイムスタンプは重複させない
        near = _nearest_cut(t, cuts)
        if near is not None and abs(near - t) <= 0.15:
            _push_event(events, seen_ts, t, "transition", "cut", 0.85)
            continue
        _push_event(events, seen_ts, t, "shimmer", "none", 0.4)

    events.sort(key=lambda e: e["t"])
    return events


def _push_event(events, seen_ts, t, kind, anchor, confidence):
    key = (round(float(t), 2), kind)
    if key in seen_ts:
        return
    seen_ts[key] = True
    events.append({"t": round(float(t), 3), "kind": kind, "anchor": anchor, "confidence": confidence})


def run_ffmpeg_ametadata(
    ffmpeg_bin: str,
    audio_path: str,
    highpass_hz: Optional[float],
    window_samples: int = 2048,
    timeout_sec: int = 120,
) -> str:
    """astats+ametadata 経路を実行して stderr を返す（実実行）。"""
    filters = []
    if highpass_hz:
        filters.append("highpass=f={:.0f}".format(float(highpass_hz)))
    filters.append("asetnsamples=n={}:p=0".format(int(window_samples)))
    filters.append("astats=metadata=1:reset=1")
    filters.append("ametadata=print:key=lavfi.astats.Overall.RMS_level")
    af = ",".join(filters)
    cmd = [
        str(ffmpeg_bin), "-hide_banner", "-nostats", "-i", audio_path,
        "-af", af, "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
    # ametadata print は stderr 側に出る
    return (proc.stderr or b"").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Vision 呼び出し (バッチ)
# ---------------------------------------------------------------------------

def _load_prompt(name):
    path = os.path.join(_PROMPT_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_vision_prompt(frames_batch: List[Dict[str, Any]]) -> str:
    template = _load_prompt(_VISION_PROMPT_FILE)
    lines = []
    for i, fr in enumerate(frames_batch, start=1):
        lines.append("- 画像{} (index={}, t={:.2f}s): {}".format(i, fr.get("index"), fr.get("time", 0.0), fr.get("path")))
    return template.replace("{IMAGE_LIST_BLOCK}", "\n".join(lines))


def analyze_frames_with_vision(
    frames: List[Dict[str, Any]],
    cfg: Optional[dict] = None,
    vision_call=None,
    progress_cb=None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """フレーム群をバッチで vision に投げ、フレーム別JSONと warnings を返す。"""
    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    batch = int(ref_cfg.get("vision_batch_size", 10))
    max_calls = int(ref_cfg.get("max_vision_calls", 4))
    timeout_sec = int(ref_cfg.get("vision_timeout_sec", 600))
    vision_call = vision_call if vision_call is not None else call_claude_vision_json
    warnings: List[str] = []
    results: List[Dict[str, Any]] = []
    if vision_call is None:
        warnings.append("vision呼び出しが利用できませんでした(claude_runner未整備)")
        return results, warnings

    calls_used = 0
    for i in range(0, len(frames), batch):
        if calls_used >= max_calls:
            warnings.append("vision呼び出し回数の上限({})に達したため残りフレームをスキップ".format(max_calls))
            break
        batch_frames = frames[i:i + batch]
        prompt = build_vision_prompt(batch_frames)
        try:
            paths = [f["path"] for f in batch_frames if f.get("path")]
            result = vision_call(prompt, paths, timeout_sec=timeout_sec)
        except Exception as exc:
            warnings.append("vision呼び出しで例外: {}".format(str(exc)[:200]))
            calls_used += 1
            continue
        calls_used += 1
        if progress_cb:
            try:
                progress_cb("vision_batch", {"call": calls_used, "frames": len(batch_frames)})
            except Exception:
                pass
        if not result or not result.get("ok"):
            warnings.append("visionバッチ失敗(call={}): {}".format(calls_used, (result or {}).get("error")))
            continue
        data = result.get("data")
        if isinstance(data, list):
            for j, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                # index/time は入力側で埋め直す（LLMが取り違えても回復）
                fr = batch_frames[j] if j < len(batch_frames) else {}
                merged = {
                    "index": fr.get("index", item.get("index")),
                    "time": fr.get("time", 0.0),
                    "telop_text": (item.get("telop_text") or "").strip(),
                    "telop_position": item.get("telop_position") or "",
                    "telop_color": item.get("telop_color") or "",
                    "telop_stroke": item.get("telop_stroke") or "",
                    "emphasis_words": item.get("emphasis_words") or [],
                    "size_class": item.get("size_class") or "",
                    "visual_desc_en": item.get("visual_desc_en") or "",
                    "motion": item.get("motion") or "static",
                    "has_person": bool(item.get("has_person")),
                    "has_product_logo": bool(item.get("has_product_logo")),
                    # F3: 構造化拡張フィールド。旧プロンプトに合わせて欠落は既定値で埋める。
                    "shot_size": item.get("shot_size") or "",
                    "subject_count": int(item.get("subject_count")) if isinstance(item.get("subject_count"), (int, float)) else 0,
                    "camera_move": item.get("camera_move") or "",
                    "color_mood": item.get("color_mood") or "",
                }
                results.append(merged)
        else:
            warnings.append("visionレスポンスがJSON配列ではない(call={})".format(calls_used))

    return results, warnings


# ---------------------------------------------------------------------------
# 融合プロンプト
# ---------------------------------------------------------------------------

def build_fusion_prompt(
    url: str,
    duration_sec: float,
    transcript: str,
    segments: List[Dict[str, Any]],
    asr_analysis: Dict[str, Any],
    cuts: List[float],
    vision_results: List[Dict[str, Any]],
    onsets: List[Dict[str, Any]],
) -> str:
    template = _load_prompt(_FUSION_PROMPT_FILE)
    return (
        template
        .replace("{URL}", url or "")
        .replace("{DURATION}", "{:.2f}".format(float(duration_sec or 0.0)))
        .replace("{TRANSCRIPT}", (transcript or "")[:4000])
        .replace("{SEGMENTS_JSON}", json.dumps(segments or [], ensure_ascii=False))
        .replace("{ASR_ANALYSIS_JSON}", json.dumps(asr_analysis or {}, ensure_ascii=False))
        .replace("{CUTS_JSON}", json.dumps([{"t": round(float(c), 3)} for c in cuts or []], ensure_ascii=False))
        .replace("{VISION_RESULTS_JSON}", json.dumps(vision_results or [], ensure_ascii=False))
        .replace("{ONSETS_JSON}", json.dumps(onsets or [], ensure_ascii=False))
    )


# ---------------------------------------------------------------------------
# validate v2 spec
# ---------------------------------------------------------------------------

_V2_REQUIRED_KEYS = (
    "version", "url", "duration_sec", "transcript", "segments", "beats", "rhythm",
    "cuts", "shots_ref", "telops", "sfx_events", "bgm", "warnings",
)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_reference_spec_v2(spec: Any) -> Tuple[bool, List[str], Optional[Dict[str, Any]]]:
    """v2 spec を検査し (ok, errors, normalized_spec) を返す。"""
    errors: List[str] = []
    if not isinstance(spec, dict):
        return False, ["spec はオブジェクトである必要があります"], None

    missing = [k for k in _V2_REQUIRED_KEYS if k not in spec]
    if missing:
        errors.append("必須キーが不足しています: {}".format(missing))

    duration = spec.get("duration_sec")
    if not _is_number(duration) or float(duration) <= 0:
        errors.append("duration_sec は正の数値である必要があります(got: {!r})".format(duration))
        duration_f = 0.0
    else:
        duration_f = float(duration)

    if spec.get("version") != 2:
        errors.append("version は 2 である必要があります(got: {!r})".format(spec.get("version")))

    # cuts: [{t, confidence}]、時刻は単調非減少、range内
    cuts = spec.get("cuts") or []
    if not isinstance(cuts, list):
        errors.append("cuts はリストである必要があります")
        cuts = []
    prev_t = -1.0
    for i, c in enumerate(cuts):
        if not isinstance(c, dict):
            errors.append("cuts[{}] はオブジェクトではありません".format(i))
            continue
        t = c.get("t")
        if not _is_number(t):
            errors.append("cuts[{}].t が数値ではありません".format(i))
            continue
        tf = float(t)
        if tf < 0 or (duration_f and tf > duration_f + 0.5):
            errors.append("cuts[{}].t が [0, duration] を超えています(t={})".format(i, tf))
        if tf < prev_t - 1e-3:
            errors.append("cuts[{}].t は単調非減少である必要があります(prev={}, curr={})".format(i, prev_t, tf))
        prev_t = tf

    # telops
    for i, tel in enumerate(spec.get("telops") or []):
        if not isinstance(tel, dict):
            errors.append("telops[{}] はオブジェクトではありません".format(i))
            continue
        s = tel.get("start")
        e = tel.get("end")
        if not _is_number(s) or not _is_number(e):
            errors.append("telops[{}] の start/end が数値ではありません".format(i))
            continue
        if float(e) < float(s) - 1e-3:
            errors.append("telops[{}] の end<start です".format(i))
        if duration_f and (float(s) < -0.1 or float(e) > duration_f + 0.5):
            errors.append("telops[{}] の秒が [0, duration] を超えています".format(i))

    # sfx_events
    prev_t = -1.0
    for i, ev in enumerate(spec.get("sfx_events") or []):
        if not isinstance(ev, dict):
            errors.append("sfx_events[{}] はオブジェクトではありません".format(i))
            continue
        t = ev.get("t")
        if not _is_number(t):
            errors.append("sfx_events[{}].t が数値ではありません".format(i))
            continue
        tf = float(t)
        if duration_f and (tf < -0.1 or tf > duration_f + 0.5):
            errors.append("sfx_events[{}].t が [0, duration] を超えています".format(i))
        if tf < prev_t - 1e-3:
            errors.append("sfx_events[{}].t は単調非減少である必要があります".format(i))
        prev_t = tf
        kind = ev.get("kind")
        if kind not in ("transition", "impact", "riser", "pop", "shimmer", "other"):
            errors.append("sfx_events[{}].kind が不正です(got: {!r})".format(i, kind))

    if errors:
        return False, errors, None
    # 正常化(コピー)
    normalized = dict(spec)
    normalized.setdefault("warnings", [])
    return True, [], normalized


# ---------------------------------------------------------------------------
# 中間素材の掃除
# ---------------------------------------------------------------------------

def _is_cleanable_v2_tmp_dir(tmp_dir: Optional[str]) -> bool:
    if not tmp_dir or not isinstance(tmp_dir, str):
        return False
    try:
        base = os.path.basename(os.path.normpath(tmp_dir))
    except Exception:
        return False
    return base.startswith(_TMP_DIR_CLEANUP_PREFIX_V2)


def _cleanup(tmp_dir: Optional[str]) -> None:
    if _is_cleanable_v2_tmp_dir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# メイン: analyze_reference_v2
# ---------------------------------------------------------------------------

def _run_asr_and_analysis(
    audio_path: str,
    duration_sec: float,
    cfg: dict,
    asr_post,
    claude_call,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """v1 の ASR+claude分析を音声パスで再実行し (asr_result_and_analysis, warnings) を返す。"""
    warnings: List[str] = []
    asr = asr_post(audio_path, cfg)
    if not asr or not asr.get("ok"):
        warnings.append("ASR失敗: {}".format((asr or {}).get("error") or "unknown"))
        return None, warnings
    transcript = asr.get("text") or ""
    segments = asr.get("segments") or []
    if not duration_sec:
        duration_sec = asr.get("duration") or 0.0
    prompt = ref_v1.build_reference_analysis_prompt(transcript, segments, duration_sec)
    timeout_sec = cfg.get("claude_timeout_sec", 600)
    try:
        result = claude_call(prompt, timeout_sec=timeout_sec) if claude_call is not None else None
    except Exception as exc:
        warnings.append("claude構成解析で例外: {}".format(str(exc)[:200]))
        return None, warnings
    if not result or not result.get("ok") or not isinstance(result.get("data"), dict):
        warnings.append("claude構成解析失敗: {}".format((result or {}).get("error") or "unknown"))
        return {
            "transcript": transcript, "segments": segments, "duration_sec": duration_sec,
            "beats": [], "rhythm": None,
        }, warnings
    data = result["data"]
    return {
        "transcript": transcript,
        "segments": segments,
        "duration_sec": duration_sec,
        "beats": data.get("beats") or [],
        "rhythm": data.get("rhythm"),
    }, warnings


def analyze_reference_v2(
    url,
    cfg=None,
    progress_cb=None,
    fetch_video=None,
    detect_cuts=None,
    extract_frames=None,
    vision_call=None,
    ffmpeg_run_ametadata=None,
    asr_post=None,
    claude_call=None,
    fusion_call=None,
):
    """参考動画URLをフルDLして多モーダル解析し spec v2 を返す（例外を投げない）。

    Returns:
        {"ok": bool, "spec": dict|None, "source": "cache"|"multimodal"|None,
         "cached": bool, "warnings": [str,...], "error": str|None,
         "meta": {"cuts_count": int, "vision_calls": int, ...}}
    """
    cfg = cfg or load_config()
    warnings: List[str] = []
    meta: Dict[str, Any] = {}

    def _progress(stage, detail=None):
        if progress_cb is None:
            return
        try:
            progress_cb(stage, detail)
        except Exception:
            pass

    normalized = ref_v1.normalize_url(url)
    if not normalized:
        return {
            "ok": False, "spec": None, "source": None, "cached": False,
            "warnings": warnings, "error": "参考動画のURLが不正です", "meta": meta,
        }

    cache_path = _cache_path_for_v2(normalized, cfg)
    _progress("cache_check")
    cached = _load_cache(cache_path)
    if cached is not None:
        return {"ok": True, "spec": cached, "source": "cache", "cached": True, "warnings": [], "error": None, "meta": meta}

    fetch_video_fn = fetch_video or default_fetch_video
    detect_cuts_fn = detect_cuts  # 実行時は ffmpeg_bin と閾値も引き回す
    extract_frames_fn = extract_frames or extract_frames_at_times
    ametadata_fn = ffmpeg_run_ametadata or run_ffmpeg_ametadata
    asr_post_fn = asr_post or ref_v1.default_asr_post
    claude_call_fn = claude_call if claude_call is not None else call_claude_json
    fusion_call_fn = fusion_call if fusion_call is not None else call_claude_json

    _progress("download")
    try:
        video_info = fetch_video_fn(normalized, cfg)
    except Exception as exc:
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画のDLに失敗しました: {}".format(exc), "meta": meta,
        }

    tmp_dir = (video_info or {}).get("tmp_dir")
    video_path = (video_info or {}).get("video_path")
    audio_path = (video_info or {}).get("audio_path")
    duration_sec = float((video_info or {}).get("duration_sec") or 0.0)
    ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")

    try:
        if not video_path or not audio_path:
            raise RuntimeError("fetch_video が video_path / audio_path を返しませんでした")

        # -----------------------------------------------------------------
        # カット検出
        # -----------------------------------------------------------------
        _progress("detect_cuts")
        ref_cfg = cfg.get("reference") or {}
        threshold = float(ref_cfg.get("scene_threshold", 0.30))
        if detect_cuts_fn is None:
            cuts = detect_cuts_via_ffmpeg(ffmpeg_bin, video_path, threshold)
        else:
            cuts = detect_cuts_fn(video_path, threshold)
        if not cuts:
            cuts = synth_cuts_uniform(duration_sec, interval=5.0)
            if cuts:
                warnings.append("カット検出0件のため 5秒等分の擬似カットを生成しました")
        meta["cuts_count"] = len(cuts)

        # -----------------------------------------------------------------
        # フレーム抽出 + Vision
        # -----------------------------------------------------------------
        _progress("extract_frames")
        max_frames = int(ref_cfg.get("max_frames", 40))
        frame_times = select_frame_times_from_cuts(cuts, duration_sec, max_frames=max_frames)
        frames_dir = os.path.join(tmp_dir or tempfile.gettempdir(), "frames")
        if extract_frames_fn is extract_frames_at_times:
            frames = extract_frames_fn(ffmpeg_bin, video_path, frame_times, frames_dir)
        else:
            frames = extract_frames_fn(video_path, frame_times, frames_dir)
        meta["frames_extracted"] = len(frames)

        _progress("vision")
        vision_results, vision_warnings = analyze_frames_with_vision(
            frames, cfg=cfg, vision_call=vision_call, progress_cb=progress_cb
        )
        warnings.extend(vision_warnings)
        meta["vision_results"] = len(vision_results)

        # フレームPNGは以後不要 → 即削除
        for f in frames:
            try:
                os.remove(f["path"])
            except OSError:
                pass
        shutil.rmtree(frames_dir, ignore_errors=True)

        # -----------------------------------------------------------------
        # 音声オンセット検出（ASRは後段で使うため、音声ファイルはまだ消さない）
        # -----------------------------------------------------------------
        _progress("onsets")
        window_samples = int(ref_cfg.get("onset_window_samples", 2048))
        jump_db = float(ref_cfg.get("onset_jump_db", 6.0))
        try:
            hf_stderr = ametadata_fn(ffmpeg_bin, audio_path, 2000.0, window_samples=window_samples)
            hf_series = parse_ametadata_stderr(hf_stderr)
            hf_onsets = detect_onsets_from_rms_series(hf_series, jump_db=jump_db)
        except Exception as exc:
            warnings.append("HFオンセット検出失敗: {}".format(str(exc)[:200]))
            hf_onsets = []
        try:
            full_stderr = ametadata_fn(ffmpeg_bin, audio_path, None, window_samples=window_samples)
            full_series = parse_ametadata_stderr(full_stderr)
            full_onsets = detect_onsets_from_rms_series(full_series, jump_db=jump_db)
        except Exception as exc:
            warnings.append("全帯域オンセット検出失敗: {}".format(str(exc)[:200]))
            full_onsets = []

        # -----------------------------------------------------------------
        # ASR + ビート分析（オンセット分類の発話除外にも使う）
        # -----------------------------------------------------------------
        _progress("asr")
        asr_and_analysis, asr_warnings = _run_asr_and_analysis(
            audio_path, duration_sec, cfg, asr_post_fn, claude_call_fn
        )
        warnings.extend(asr_warnings)
        if asr_and_analysis is None:
            # ASR不可(APIキー未設定・接続断など)でも v2 の主眼(cuts/telops/sfx_events)は
            # 映像+ffmpeg側で得られるので、transcript/segments/beats を空で埋めて融合を続行する。
            warnings.append("ASR不能のため transcript/beats を空で継続します")
            asr_and_analysis = {
                "transcript": "",
                "segments": [],
                "duration_sec": duration_sec,
                "beats": [],
                "rhythm": None,
            }

        speech_segments = asr_and_analysis.get("segments") or []
        onsets = classify_onsets(cuts, speech_segments, hf_onsets, full_onsets)
        meta["onsets_count"] = len(onsets)

        # -----------------------------------------------------------------
        # 融合 (fusion prompt)
        # -----------------------------------------------------------------
        _progress("fusion")
        fusion_prompt = build_fusion_prompt(
            normalized,
            duration_sec,
            asr_and_analysis.get("transcript") or "",
            speech_segments,
            {"beats": asr_and_analysis.get("beats") or [], "rhythm": asr_and_analysis.get("rhythm")},
            cuts,
            vision_results,
            onsets,
        )
        try:
            fusion_result = fusion_call_fn(fusion_prompt, timeout_sec=cfg.get("claude_timeout_sec", 600))
        except Exception as exc:
            return {
                "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
                "error": "融合(claude)で例外: {}".format(str(exc)[:200]), "meta": meta,
            }
        if not fusion_result or not fusion_result.get("ok") or not isinstance(fusion_result.get("data"), dict):
            return {
                "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
                "error": "融合(claude)応答が取得できませんでした: {}".format((fusion_result or {}).get("error")),
                "meta": meta,
            }

        spec = _normalize_v2_from_llm(
            fusion_result["data"], normalized, duration_sec, asr_and_analysis, cuts, onsets, warnings
        )
        ok, errors, normalized_spec = validate_reference_spec_v2(spec)
        if not ok:
            # 矯正1回: 決定的に補完してから再検査
            corrected = _correct_v2_spec(spec, duration_sec, cuts, onsets)
            ok2, errors2, normalized_spec = validate_reference_spec_v2(corrected)
            if not ok2:
                return {
                    "ok": False, "spec": None, "source": None, "cached": False,
                    "warnings": warnings + errors + errors2,
                    "error": "spec v2 の検証に失敗しました", "meta": meta,
                }

        _write_cache(cache_path, normalized_spec)
        _progress("done")
        return {
            "ok": True, "spec": normalized_spec, "source": "multimodal",
            "cached": False, "warnings": warnings, "error": None, "meta": meta,
        }
    except Exception as exc:
        return {
            "ok": False, "spec": None, "source": None, "cached": False, "warnings": warnings,
            "error": "参考動画v2解析で例外: {}".format(str(exc)[:300]), "meta": meta,
        }
    finally:
        _cleanup(tmp_dir)


def _normalize_v2_from_llm(
    data: Dict[str, Any],
    url: str,
    duration_sec: float,
    asr_and_analysis: Dict[str, Any],
    cuts: List[float],
    onsets: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    """LLM出力に対して欠落キーを埋め、スキーマ形へ整える。"""
    spec = dict(data) if isinstance(data, dict) else {}
    spec["version"] = 2
    spec["url"] = url
    spec["duration_sec"] = float(duration_sec or 0.0)
    spec.setdefault("transcript", asr_and_analysis.get("transcript") or "")
    spec.setdefault("segments", asr_and_analysis.get("segments") or [])
    spec.setdefault("beats", asr_and_analysis.get("beats") or [])
    spec.setdefault("rhythm", asr_and_analysis.get("rhythm"))
    if not isinstance(spec.get("cuts"), list) or not spec["cuts"]:
        spec["cuts"] = [{"t": round(float(c), 3), "confidence": 0.8} for c in cuts]
    if not isinstance(spec.get("shots_ref"), list):
        spec["shots_ref"] = []
    if not isinstance(spec.get("telops"), list):
        spec["telops"] = []
    if not isinstance(spec.get("sfx_events"), list) or not spec["sfx_events"]:
        spec["sfx_events"] = onsets
    if not isinstance(spec.get("bgm"), dict):
        spec["bgm"] = {"present": False, "mood_guess": ""}
    if not isinstance(spec.get("warnings"), list):
        spec["warnings"] = []
    spec["warnings"] = list(spec.get("warnings") or []) + list(warnings or [])
    return spec


def _correct_v2_spec(
    spec: Dict[str, Any], duration_sec: float, cuts: List[float], onsets: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """検証失敗時の決定的矯正: cutsは検出結果で上書き、範囲外時刻はクランプ。"""
    corrected = dict(spec)
    corrected["version"] = 2
    corrected["duration_sec"] = float(duration_sec or 0.0)
    corrected["cuts"] = [{"t": round(float(c), 3), "confidence": 0.8} for c in cuts]
    # sfx_events を検出結果で上書き
    corrected["sfx_events"] = onsets
    # telops のクランプ
    fixed_telops = []
    for tel in corrected.get("telops") or []:
        if not isinstance(tel, dict):
            continue
        s = tel.get("start")
        e = tel.get("end")
        if not _is_number(s) or not _is_number(e):
            continue
        s = max(0.0, min(float(duration_sec or 0.0), float(s)))
        e = max(s, min(float(duration_sec or 0.0), float(e)))
        tel = dict(tel)
        tel["start"] = s
        tel["end"] = e
        fixed_telops.append(tel)
    corrected["telops"] = fixed_telops
    corrected.setdefault("shots_ref", [])
    corrected.setdefault("bgm", {"present": False, "mood_guess": ""})
    corrected.setdefault("warnings", [])
    return corrected
