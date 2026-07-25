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
from pipeline import music as music_mod  # R2a F5: BPM/beat 抽出
from pipeline import optical_flow as of_mod  # R2b F2: 光学フローによる camera_move 機械判定
from pipeline import telop_refine as telop_refine_mod  # R2b F8: テロップ帯変化検出
from pipeline import narration_rhythm as narration_rhythm_mod  # R2b F7: ナレーション話速統計
from pipeline import sfx_matcher as sfx_matcher_mod  # R2b F6: SE音色 MFCC


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


def detect_cuts_via_pyscenedetect(video_path: str, threshold: float = 27.0) -> Optional[List[float]]:
    """PySceneDetect(ContentDetector) でカット秒列を返す（ディゾルブ耐性）。

    PySceneDetect の ContentDetector は HSV差分の重み付き平均を使いカット判定するため、
    ffmpeg の scdet(輝度差主体) が拾えない徐々に切り替わるディゾルブ・薄いフラッシュを
    拾いやすい。threshold は 0..255 の内部単位（ContentDetector 既定は 27）。

    Returns:
        list[float]: 検出成功時の内部境界秒列（0.0 は含めない・時系列昇順）。
        None: PySceneDetect が未インストール、または video を開けなかった（＝検出器
              として不作動）ことを表す。**空リストとは意味が違う**（空リストは
              「検出器が動いたがカット0件」）。合議 confidence 計算で不作動検出器を
              分母から除外するため、この区別を保持する（→ merge_cut_lists）。
    """
    try:
        from scenedetect import SceneManager, open_video  # type: ignore
        from scenedetect.detectors import ContentDetector  # type: ignore
    except Exception:
        return None
    try:
        video = open_video(str(video_path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=float(threshold)))
        sm.detect_scenes(video, show_progress=False)
        scenes = sm.get_scene_list()
    except Exception:
        return None
    # get_scene_list は [(start, end), ...] を返す。カット時刻＝各シーンの start（先頭を除く）。
    out = []
    for i, (start, _end) in enumerate(scenes or []):
        if i == 0:
            continue
        try:
            t = float(start.get_seconds())
        except Exception:
            continue
        if t > 0.0:
            out.append(round(t, 3))
    return sorted(out)


def merge_cut_lists(
    cut_lists: List[Optional[List[float]]],
    merge_window_sec: float = 0.15,
) -> List[Dict[str, Any]]:
    """複数手法のカット秒列をアンサンブル統合する。

    Args:
        cut_lists: 各検出器の出力。**不作動の検出器は None** を渡す（未インストール・
            例外発生など）。**動いたが 0 件検出**は空リスト `[]` を渡す。この区別を
            尊重して分母（合意可能な検出器数）から不作動を除外する。
        merge_window_sec: 秒差がこれ以内なら 1グループにマージ。

    Confidence 計算:
        - 分母 = 「不作動でない」検出器の数（None を除いたリスト数）。
        - 分子 = そのグループで少なくとも1件を寄稿した検出器のユニーク数。
        （P2 修正: 不作動検出器を分母に入れると unanimous でも 0.75 にしかならず
        high 判定に届かない問題を修正。）

    Returns: [{"t": float, "confidence": float, "sources": int}]（t 昇順）
    """
    if not cut_lists:
        return []
    # None（不作動）は分母から除外し、以降のインデックス操作にも参加させない。
    active_indexed: List[Tuple[int, List[float]]] = []
    for i, lst in enumerate(cut_lists):
        if lst is None:
            continue
        active_indexed.append((i, lst))
    total_detectors = len(active_indexed)
    if total_detectors == 0:
        return []
    # 各カットに「どの検出器由来か」を付ける（不作動を除外した active リスト経由）。
    tagged: List[Tuple[float, int]] = []
    for det_idx, lst in active_indexed:
        if not lst:
            continue
        for t in lst:
            try:
                tf = float(t)
            except Exception:
                continue
            if tf <= 0.0:
                continue
            tagged.append((tf, det_idx))
    if not tagged:
        return []
    tagged.sort(key=lambda x: x[0])
    groups: List[List[Tuple[float, int]]] = []
    for entry in tagged:
        if not groups or (entry[0] - groups[-1][-1][0]) > merge_window_sec:
            groups.append([entry])
        else:
            groups[-1].append(entry)
    out: List[Dict[str, Any]] = []
    for g in groups:
        avg_t = sum(e[0] for e in g) / float(len(g))
        detectors = len(set(e[1] for e in g))
        confidence = round(detectors / float(total_detectors), 3)
        out.append({"t": round(avg_t, 3), "confidence": confidence, "sources": detectors})
    return out


def detect_cuts_ensemble(
    ffmpeg_bin: str,
    video_path: str,
    scene_thresholds: Optional[List[float]] = None,
    use_pyscenedetect: bool = True,
    pyscenedetect_threshold: float = 27.0,
    merge_window_sec: float = 0.15,
    timeout_sec: int = 120,
) -> List[Dict[str, Any]]:
    """マルチ手法カット検出のアンサンブル。

    - scene_thresholds: ffmpeg scdet を回す閾値の並び（既定 [0.20, 0.30, 0.40]）。
    - use_pyscenedetect: PySceneDetect ContentDetector も並行実行して統合するか。
      True かつ PySceneDetect が import できるとき有効。
    - merge_window_sec: 秒差がこれ以内のカットを1つに統合し confidence を上げる。

    Returns: [{"t": float, "confidence": float, "sources": int}]
    """
    thresholds = list(scene_thresholds) if scene_thresholds else [0.20, 0.30, 0.40]
    cut_lists: List[Optional[List[float]]] = []
    for thr in thresholds:
        try:
            cuts = detect_cuts_via_ffmpeg(ffmpeg_bin, video_path, float(thr), timeout_sec=timeout_sec)
        except Exception:
            # ffmpeg 検出器そのものが失敗（video 破損など）→ 不作動として None を積む
            # ことで confidence の分母から除外する（P2 修正）。
            cuts = None
        cut_lists.append(cuts)
    if use_pyscenedetect:
        try:
            pcs = detect_cuts_via_pyscenedetect(video_path, threshold=pyscenedetect_threshold)
        except Exception:
            pcs = None
        # detect_cuts_via_pyscenedetect は不作動時 None を返す（未インストール・openエラー）
        cut_lists.append(pcs)
    return merge_cut_lists(cut_lists, merge_window_sec=merge_window_sec)


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

def select_frame_times_from_cuts(
    cuts: List[float],
    duration_sec: float,
    max_frames: int = 40,
    frames_per_shot: int = 2,
) -> List[float]:
    """各カット区間 (前カット, 次カット) から `frames_per_shot` 枚のフレーム秒列を返す。

    - frames_per_shot=2 (既定): 直後+0.2s / 区間中央 の2枚。旧挙動と後方互換。
    - frames_per_shot=3 (P2 拡張・supreme_plus 用): 直後+0.2s / 区間中央 / 区間末-0.2s の
      3枚を返し、色調・光・被写体の変化を細かく捉える。区間が短くて末端フレームが
      中央と重なる場合は自動間引き（同一秒を dedup）。
    - frames_per_shot>=4 は請求せず 3 にクリップ（vision コストが跳ねるため保険）。

    上限 max_frames を超える場合は等間引きする。cuts は0秒を含まない前提（先頭区間は [0, cuts[0]]）。
    """
    fps_shot = max(1, min(3, int(frames_per_shot or 2)))
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
        seg_len = seg_end - seg_start
        if seg_len < 0.1:
            continue
        # 直後 +0.2s (区間長が0.4s未満なら 区間長の1/4)
        head_off = 0.2 if seg_len >= 0.4 else max(0.05, seg_len / 4.0)
        t1 = seg_start + head_off
        times.append(round(min(t1, seg_end - 0.02), 3))
        if fps_shot >= 2:
            t2 = seg_start + seg_len / 2.0
            if abs(t2 - t1) > 0.1:
                times.append(round(min(t2, seg_end - 0.02), 3))
        if fps_shot >= 3 and seg_len >= 0.6:
            # 区間末尾 -0.2s（ただし head/mid と重ならない場合のみ）
            tail_off = 0.2 if seg_len >= 0.6 else max(0.05, seg_len / 4.0)
            t3 = seg_end - tail_off
            if all(abs(t3 - t) > 0.1 for t in (times[-2:] if len(times) >= 2 else times)):
                times.append(round(max(t3, seg_start + 0.02), 3))
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


_ALLOWED_FRAMING_TOKENS = {
    "rule_of_thirds", "center", "symmetric", "low_angle", "high_angle", "eye_level",
    "over_the_shoulder", "aerial", "dutch_angle", "top_down", "closeup_face",
    "product_on_hand", "flatlay",
}


def _normalize_palette_hex(raw: Any) -> List[str]:
    """vision 応答の color_palette_hex を "#rrggbb" のリストに正規化する。

    - 数値・空文字・None は捨てる
    - "0xFFAA00" / "FFAA00" / "#ffaa00" などの表記ゆれを "#ffaa00" に統一
    - 最大 3 色（重複は除去、順序は保持）
    """
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for c in raw:
        if not isinstance(c, str):
            continue
        s = c.strip().lower()
        if s.startswith("0x"):
            s = "#" + s[2:]
        elif not s.startswith("#"):
            s = "#" + s
        # #RRGGBB 6桁のみ受理（透明度・短縮は捨てる）
        if len(s) != 7:
            continue
        if not all(ch in "0123456789abcdef" for ch in s[1:]):
            continue
        if s not in out:
            out.append(s)
        if len(out) >= 3:
            break
    return out


def _resolve_frames_per_shot(cfg: Optional[Dict[str, Any]]) -> int:
    """cfg から frames_per_shot を決める。

    優先順位:
      1. `reference.frames_per_shot` が明示されていればその値
      2. `director_quality == "supreme_plus"` なら 3
      3. それ以外は 2（従来挙動）
    """
    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    explicit = ref_cfg.get("frames_per_shot")
    if isinstance(explicit, int) and explicit >= 1:
        return min(3, explicit)
    quality = (cfg.get("director_quality") or "").strip().lower()
    if quality == "supreme_plus":
        return 3
    return 2


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
                    # P2 拡張: 参考ショットの構図・場所・光・パレット。director の
                    # _map_reference_visual_to_shots が読み取り済みで、あとは vision
                    # プロンプト側で供給できれば shot まで貫通する。
                    "framing": (item.get("framing") or "").strip(),
                    "location": (item.get("location") or "").strip(),
                    "lighting": (item.get("lighting") or "").strip(),
                    "color_palette_hex": _normalize_palette_hex(item.get("color_palette_hex")),
                }
                results.append(merged)
        else:
            warnings.append("visionレスポンスがJSON配列ではない(call={})".format(calls_used))

    return results, warnings


# ---------------------------------------------------------------------------
# 3秒ごと密ストーリーボード解析（診断 P1-6 / ユーザー要求「3秒に1回、1枚1枚を忠実再現」）
# ---------------------------------------------------------------------------
#
# カット境界に依らず一律 interval 秒でフレームを抽出し、1枚ずつ vision で「見えるものだけ」を
# 詳細記述する。カット内でも刻むため、長回しのショットでも進捗ゲージ等の画面内テキスト変化を
# 逃さない。結果は spec v2 に storyboard: [{t, description_ja, on_screen_text, has_person,
# person_desc, objects[]}] として付与する。ハルシネーション対策としてプロンプトで
# 「推測禁止・不明は unknown・見えないものは書かない」を明示する。

_STORYBOARD_VISION_PROMPT = (
    "あなたはショート動画のフレーム解析専門家です。以下に列挙する画像フレームを"
    "それぞれ Read ツールで読み、1枚ずつ画面の内容を記述してください。\n\n"
    "# 入力: 分析対象のフレーム画像パス一覧\n"
    "{IMAGE_LIST_BLOCK}\n\n"
    "# タスク（各画像について次を判定。前置き禁止・コードフェンス禁止・純粋なJSON配列のみ）\n"
    "1. description_ja: 画面全体を日本語で1〜2文。人物の性別・服装・動作、映っている物、"
    "構図、主要な色を、断定的な名詞・動詞で述べる。\n"
    "2. on_screen_text: 画面内に表示されている文字・テロップ・数字・記号を全文そのまま"
    "（例: 進捗ゲージ『10%』『100%』、価格、ロゴ文字）。複数行は \\n で連結。無ければ空文字列。\n"
    "3. has_person: 画面に人物が写っているか true/false。\n"
    "4. person_desc: 人物の性別・年齢感・服装・動作を日本語で。人物が居なければ空文字列。\n"
    "5. objects: 画面内の主要な物の名詞リスト（日本語・最大6件）。無ければ []。\n\n"
    "# 制約（ハルシネーション厳禁）\n"
    "- 実際に画面に見えるものだけを記述すること。推測・想像で補わないこと。\n"
    "- 判別できない項目は description_ja/person_desc では \"unknown\" と書き、"
    "on_screen_text は見えない文字を創作せず空文字列にすること。\n"
    "- 各要素の index は入力リストの順番と一致させること（1始まり）。\n"
    "- 出力の最初の文字は [ 、最後の文字は ] 。それ以外の文字（説明・前置き）を一切含めないこと。\n\n"
    "# 出力スキーマ（このJSON配列のみ）\n"
    "[{\"index\":1,\"description_ja\":\"…\",\"on_screen_text\":\"…\",\"has_person\":true,"
    "\"person_desc\":\"…\",\"objects\":[\"…\"]}]"
)


def compute_reference_narration_mode(spec: Dict[str, Any], asr_ok: bool) -> str:
    """参考動画の narration 有無を解析側の一次情報から確定する（統合配線）。

    戻り値: "present" / "absent" / "unknown"

    判定（ASR の成否を直接知れる解析側だからこそできる区別）:
      - transcript が非空 → "present"（セリフ実在）
      - ASR 成功 かつ transcript 空 → "absent"（ASRは動いたがセリフが無い＝元々ナレ無し）
      - ASR 失敗（残高不足/接続断等）:
          テロップが3枚以上 → "absent"（テロップ主導のナレ無し構成とみなす）
          それ未満       → "unknown"（判断保留。director はフォールバックへ委ねる）

    director._infer_narration_mode は ASR 成功/失敗を区別できないため、spec 側の
    本判定を director が優先採用する（build_shot_skeleton）。
    """
    if not isinstance(spec, dict):
        return "unknown"
    transcript = spec.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return "present"
    telops = spec.get("telops") or []
    telop_count = len([t for t in telops if isinstance(t, dict) and (t.get("text") or "").strip()])
    if asr_ok:
        return "absent"
    if telop_count >= 3:
        return "absent"
    return "unknown"


def _resolve_storyboard_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """storyboard 解析を有効化するか決める。

    優先順位:
      1. reference.storyboard_enabled が bool ならその値。
      2. director_quality が supreme_plus / ttps なら True（自動 ON）。
      3. それ以外は False。
    """
    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    explicit = ref_cfg.get("storyboard_enabled")
    if isinstance(explicit, bool):
        return explicit
    quality = (cfg.get("director_quality") or "").strip().lower()
    return quality in ("supreme_plus", "ttps")


def select_storyboard_times(duration_sec: float, interval: float = 3.0, max_frames: int = 80) -> List[float]:
    """一律 interval 秒間隔のフレーム秒列を返す（各 interval 窓の中央を狙う）。

    先頭は interval/2、以降 interval ごと。duration を超えない範囲。max_frames で頭打ち。
    duration が interval 未満なら中央1枚のみ。
    """
    d = float(duration_sec or 0.0)
    iv = max(0.5, float(interval or 3.0))
    if d <= 0:
        return []
    if d <= iv:
        return [round(d / 2.0, 3)]
    times: List[float] = []
    t = iv / 2.0
    while t < d - 0.05 and len(times) < max_frames:
        times.append(round(t, 3))
        t += iv
    return times


def build_storyboard_prompt(frames_batch: List[Dict[str, Any]]) -> str:
    """storyboard 用 vision プロンプトを、フレームパス一覧を埋めて組み立てる。"""
    lines = []
    for i, fr in enumerate(frames_batch, start=1):
        lines.append("- 画像{} (index={}, t={:.2f}s): {}".format(
            i, fr.get("index"), fr.get("time", 0.0), fr.get("path")))
    return _STORYBOARD_VISION_PROMPT.replace("{IMAGE_LIST_BLOCK}", "\n".join(lines))


def analyze_storyboard(
    frames: List[Dict[str, Any]],
    cfg: Optional[dict] = None,
    vision_call=None,
    progress_cb=None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """storyboard フレーム群をバッチで vision に投げ、1枚ごとの詳細記述リストと warnings を返す。

    各要素: {t, description_ja, on_screen_text, has_person, person_desc, objects[]}。
    index/time は入力フレーム側で埋め直す（LLM が取り違えても回復する）。
    vision 呼び出しは storyboard_max_vision_calls / storyboard_batch_size でガードする。
    """
    cfg = cfg or {}
    ref_cfg = cfg.get("reference") or {}
    batch = int(ref_cfg.get("storyboard_batch_size", 10))
    max_calls = int(ref_cfg.get("storyboard_max_vision_calls", 8))
    timeout_sec = int(ref_cfg.get("vision_timeout_sec", 600))
    vision_call = vision_call if vision_call is not None else call_claude_vision_json
    warnings: List[str] = []
    results: List[Dict[str, Any]] = []
    if vision_call is None:
        warnings.append("storyboard: vision呼び出しが利用できませんでした")
        return results, warnings
    if not frames:
        return results, warnings

    calls_used = 0
    for i in range(0, len(frames), max(1, batch)):
        if calls_used >= max_calls:
            warnings.append("storyboard: vision呼び出し上限({})に達したため残りフレームをスキップ".format(max_calls))
            break
        batch_frames = frames[i:i + batch]
        prompt = build_storyboard_prompt(batch_frames)
        try:
            paths = [f["path"] for f in batch_frames if f.get("path")]
            result = vision_call(prompt, paths, timeout_sec=timeout_sec)
        except Exception as exc:
            warnings.append("storyboard: vision例外: {}".format(str(exc)[:200]))
            calls_used += 1
            continue
        calls_used += 1
        if progress_cb:
            try:
                progress_cb("storyboard_batch", {"call": calls_used, "frames": len(batch_frames)})
            except Exception:
                pass
        if not result or not result.get("ok"):
            warnings.append("storyboard: バッチ失敗(call={}): {}".format(calls_used, (result or {}).get("error")))
            continue
        data = result.get("data")
        if not isinstance(data, list):
            warnings.append("storyboard: レスポンスがJSON配列ではない(call={})".format(calls_used))
            continue
        for j, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            fr = batch_frames[j] if j < len(batch_frames) else {}
            objs = item.get("objects")
            objs_list = [str(o).strip() for o in objs if str(o).strip()][:6] if isinstance(objs, list) else []
            results.append({
                "t": float(fr.get("time", 0.0)),
                "description_ja": (item.get("description_ja") or "").strip(),
                "on_screen_text": (item.get("on_screen_text") or "").strip(),
                "has_person": bool(item.get("has_person")),
                "person_desc": (item.get("person_desc") or "").strip(),
                "objects": objs_list,
            })
    results.sort(key=lambda r: r.get("t", 0.0))
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
    music: Optional[Dict[str, Any]] = None,
) -> str:
    """R2a F5: music={bpm,beat_times,confidence,downbeats} を任意で受け取り
    fusion prompt に注入する。テンプレに {MUSIC_JSON} が無ければ何もしない
    （後方互換: 既存のfusion prompt テンプレートが古くても壊れない）。
    """
    template = _load_prompt(_FUSION_PROMPT_FILE)
    music_payload = music or {"bpm": 0.0, "beat_times": [], "downbeats": [], "confidence": 0.0}
    # beat_times は長いので上限で切り詰めて渡す（LLMは平均値だけ理解できれば十分）。
    trimmed_music = dict(music_payload)
    bt = trimmed_music.get("beat_times") or []
    if len(bt) > 200:
        trimmed_music["beat_times"] = bt[:200]
        trimmed_music["beat_times_truncated"] = True
    filled = (
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
    # {MUSIC_JSON} プレースホルダがテンプレに無い場合は置換なしでそのまま返す（後方互換）。
    if "{MUSIC_JSON}" in filled:
        filled = filled.replace("{MUSIC_JSON}", json.dumps(trimmed_music, ensure_ascii=False))
    return filled


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

    # R2a F5: music は任意フィールド。dictならBPM/beat_timesの型のみゆるく確認する。
    music = spec.get("music")
    if music is not None:
        if not isinstance(music, dict):
            errors.append("music はオブジェクトである必要があります")
        else:
            bpm = music.get("bpm")
            if bpm is not None and not _is_number(bpm):
                errors.append("music.bpm は数値である必要があります(got: {!r})".format(bpm))
            bt = music.get("beat_times")
            if bt is not None and not isinstance(bt, list):
                errors.append("music.beat_times はリストである必要があります")
            conf = music.get("confidence")
            if conf is not None and not _is_number(conf):
                errors.append("music.confidence は数値である必要があります(got: {!r})".format(conf))

    # storyboard は任意フィールド。list なら各要素の t / 必須キーの型のみゆるく確認する。
    storyboard = spec.get("storyboard")
    if storyboard is not None:
        if not isinstance(storyboard, list):
            errors.append("storyboard はリストである必要があります")
        else:
            prev_t = -1.0
            for i, sb in enumerate(storyboard):
                if not isinstance(sb, dict):
                    errors.append("storyboard[{}] はオブジェクトではありません".format(i))
                    continue
                t = sb.get("t")
                if not _is_number(t):
                    errors.append("storyboard[{}].t が数値ではありません".format(i))
                    continue
                tf = float(t)
                if duration_f and (tf < -0.1 or tf > duration_f + 0.5):
                    errors.append("storyboard[{}].t が [0, duration] を超えています(t={})".format(i, tf))
                if tf < prev_t - 1e-3:
                    errors.append("storyboard[{}].t は単調非減少である必要があります".format(i))
                prev_t = tf
                if "on_screen_text" in sb and not isinstance(sb.get("on_screen_text"), str):
                    errors.append("storyboard[{}].on_screen_text は文字列である必要があります".format(i))
                if "objects" in sb and not isinstance(sb.get("objects"), list):
                    errors.append("storyboard[{}].objects はリストである必要があります".format(i))

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


def _asr_disabled_post(audio_path, cfg=None, **_kw):
    """0円保証（むりょうコース）用の no-op ASR: 外部へ一切アクセスせず「ASR不能」を返す。"""
    return {"ok": False, "error": "asr_disabled_free_tier"}


def resolve_asr_post(asr_post_fn, cfg):
    """cfg["reference"]["asr_enabled"] が False のとき、外部（Fish Audio ASR=有料）を呼ばない
    no-op ASR に差し替える。それ以外（True/未指定）は渡された asr_post_fn をそのまま使う。

    plan_tier="free" のとき pipeline.plan_tier.apply_tier_to_cfg が asr_enabled=False を立てる。
    明示的に asr_post を渡していても、asr_enabled=False が最優先（0円保証を機械的に担保するため）。
    """
    if ((cfg or {}).get("reference") or {}).get("asr_enabled") is False:
        return _asr_disabled_post
    return asr_post_fn


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
        # ★codex-review P2 対策: cache キーは URL のみ＝解析モードを含まない。storyboard を
        # 後から有効化（supreme_plus/ttps 切替や reference.storyboard_enabled=True）しても、
        # storyboard を持たない旧 cache がそのまま返ると新機能が握り潰される。storyboard が
        # 要求されているのに cache に storyboard キーが無いときは cache を使わず再解析する。
        cache_has_storyboard = isinstance(cached, dict) and ("storyboard" in cached)
        if _resolve_storyboard_enabled(cfg) and not cache_has_storyboard:
            _progress("cache_stale_storyboard")  # 再解析へフォールスルー
        else:
            return {"ok": True, "spec": cached, "source": "cache", "cached": True, "warnings": [], "error": None, "meta": meta}

    fetch_video_fn = fetch_video or default_fetch_video
    detect_cuts_fn = detect_cuts  # 実行時は ffmpeg_bin と閾値も引き回す
    extract_frames_fn = extract_frames or extract_frames_at_times
    ametadata_fn = ffmpeg_run_ametadata or run_ffmpeg_ametadata
    asr_post_fn = resolve_asr_post(asr_post or ref_v1.default_asr_post, cfg)
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
        # カット検出（R2a F1: マルチ手法アンサンブル）
        # -----------------------------------------------------------------
        _progress("detect_cuts")
        ref_cfg = cfg.get("reference") or {}
        threshold = float(ref_cfg.get("scene_threshold", 0.30))
        # cut_confidence_map: t -> confidence（アンサンブル時のみ非空。単一手法時は空dictで
        # _normalize_v2_from_llm 側の既定 0.8 が使われる）。
        cut_confidence_map: Dict[float, float] = {}
        cut_meta: Dict[str, Any] = {}
        if detect_cuts_fn is None:
            scene_thresholds = ref_cfg.get("scene_thresholds")
            use_pcs = bool(ref_cfg.get("scene_use_pyscenedetect", True))
            if scene_thresholds or use_pcs:
                # アンサンブル: 複数閾値の ffmpeg scdet + PySceneDetect ContentDetector を統合。
                try:
                    cut_events = detect_cuts_ensemble(
                        ffmpeg_bin, video_path,
                        scene_thresholds=list(scene_thresholds) if scene_thresholds else None,
                        use_pyscenedetect=use_pcs,
                        pyscenedetect_threshold=float(ref_cfg.get("pyscenedetect_threshold", 27.0)),
                        merge_window_sec=float(ref_cfg.get("scene_merge_window_sec", 0.15)),
                    )
                except Exception as exc:
                    warnings.append("カット検出(アンサンブル)失敗、単一閾値へフォールバック: {}".format(str(exc)[:200]))
                    cut_events = []
                if cut_events:
                    cuts = [e["t"] for e in cut_events]
                    cut_confidence_map = {e["t"]: e["confidence"] for e in cut_events}
                    cut_meta["detector"] = "ensemble"
                    cut_meta["by_confidence"] = {
                        "high": sum(1 for e in cut_events if e["confidence"] >= 0.99),
                        "mid": sum(1 for e in cut_events if 0.3 < e["confidence"] < 0.99),
                    }
                else:
                    cuts = detect_cuts_via_ffmpeg(ffmpeg_bin, video_path, threshold)
                    cut_meta["detector"] = "ffmpeg_single_fallback"
            else:
                cuts = detect_cuts_via_ffmpeg(ffmpeg_bin, video_path, threshold)
                cut_meta["detector"] = "ffmpeg_single"
        else:
            cuts = detect_cuts_fn(video_path, threshold)
            cut_meta["detector"] = "injected"
        if not cuts:
            cuts = synth_cuts_uniform(duration_sec, interval=5.0)
            if cuts:
                warnings.append("カット検出0件のため 5秒等分の擬似カットを生成しました")
                cut_meta["detector"] = (cut_meta.get("detector") or "") + "+uniform_synth"
        meta["cuts_count"] = len(cuts)
        meta["cut_detector"] = cut_meta

        # -----------------------------------------------------------------
        # フレーム抽出 + Vision
        # -----------------------------------------------------------------
        _progress("extract_frames")
        max_frames = int(ref_cfg.get("max_frames", 40))
        frames_per_shot = _resolve_frames_per_shot(cfg)
        meta["frames_per_shot"] = frames_per_shot
        frame_times = select_frame_times_from_cuts(
            cuts, duration_sec, max_frames=max_frames, frames_per_shot=frames_per_shot,
        )
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

        # -----------------------------------------------------------------
        # 3秒ごと密ストーリーボード解析（P1-6・supreme_plus/ttps で自動 ON）
        # カット境界に依らず一律 interval 秒でフレームを刻み、1枚ずつ vision で詳細記述する。
        # -----------------------------------------------------------------
        storyboard_results: List[Dict[str, Any]] = []
        if _resolve_storyboard_enabled(cfg):
            _progress("storyboard")
            sb_interval = float(ref_cfg.get("storyboard_interval_sec", 3.0))
            sb_times = select_storyboard_times(duration_sec, interval=sb_interval)
            sb_dir = os.path.join(tmp_dir or tempfile.gettempdir(), "storyboard_frames")
            try:
                if extract_frames_fn is extract_frames_at_times:
                    sb_frames = extract_frames_fn(ffmpeg_bin, video_path, sb_times, sb_dir)
                else:
                    sb_frames = extract_frames_fn(video_path, sb_times, sb_dir)
            except Exception as exc:
                warnings.append("storyboard: フレーム抽出失敗: {}".format(str(exc)[:200]))
                sb_frames = []
            meta["storyboard_frames"] = len(sb_frames)
            storyboard_results, sb_warnings = analyze_storyboard(
                sb_frames, cfg=cfg, vision_call=vision_call, progress_cb=progress_cb,
            )
            warnings.extend(sb_warnings)
            meta["storyboard_entries"] = len(storyboard_results)
            for f in sb_frames:
                try:
                    os.remove(f["path"])
                except OSError:
                    pass
            shutil.rmtree(sb_dir, ignore_errors=True)

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
        # 統合配線(narration_mode): ASR が成功したか(=Noneでないか)を保持する。
        # 「ASR成功でセリフ無し→absent」と「ASR失敗→telops等から推定」を区別する材料。
        asr_ok = asr_and_analysis is not None
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
        # 音楽ビート抽出 (R2a F5): BPM と拍時刻列を取り、beat_snap の元とする
        # -----------------------------------------------------------------
        _progress("music")
        music_enabled = bool((ref_cfg.get("music_extract_enabled")
                              if "music_extract_enabled" in ref_cfg else True))
        if music_enabled:
            try:
                music_features = music_mod.extract_music_features(audio_path, cfg=cfg)
            except Exception as exc:
                music_features = {"bpm": 0.0, "beat_times": [], "downbeats": [],
                                  "confidence": 0.0, "engine": "error",
                                  "error": str(exc)[:200]}
            if music_features.get("error"):
                warnings.append("music抽出: {}".format(music_features["error"]))
        else:
            music_features = {"bpm": 0.0, "beat_times": [], "downbeats": [],
                              "confidence": 0.0, "engine": "disabled"}
        meta["music_bpm"] = music_features.get("bpm")
        meta["music_beats_count"] = len(music_features.get("beat_times") or [])
        meta["music_confidence"] = music_features.get("confidence")

        # -----------------------------------------------------------------
        # 光学フローによる camera_move 機械判定 (R2b F2)
        # -----------------------------------------------------------------
        _progress("optical_flow")
        of_enabled = bool(ref_cfg.get("optical_flow_enabled", True))
        of_shots: List[Dict[str, Any]] = []
        if of_enabled:
            try:
                of_shots = of_mod.estimate_shots(
                    video_path, cuts, duration_sec,
                    samples_per_shot=int(ref_cfg.get("optical_flow_samples_per_shot", 4)),
                    analysis_width=int(ref_cfg.get("optical_flow_analysis_width", 240)),
                )
            except Exception as exc:
                warnings.append("optical_flow 失敗: {}".format(str(exc)[:200]))
                of_shots = []
        meta["optical_flow_shots"] = len(of_shots)
        # 光学フロー結果は後段の fusion で LLM shots_ref を上書きするため、変数を保持

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
            music=music_features,
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
            fusion_result["data"], normalized, duration_sec, asr_and_analysis, cuts, onsets, warnings,
            cut_confidence_map=cut_confidence_map, music=music_features,
        )
        ok, errors, normalized_spec = validate_reference_spec_v2(spec)
        if not ok:
            # 矯正1回: 決定的に補完してから再検査
            corrected = _correct_v2_spec(spec, duration_sec, cuts, onsets,
                                         cut_confidence_map=cut_confidence_map, music=music_features)
            ok2, errors2, normalized_spec = validate_reference_spec_v2(corrected)
            if not ok2:
                return {
                    "ok": False, "spec": None, "source": None, "cached": False,
                    "warnings": warnings + errors + errors2,
                    "error": "spec v2 の検証に失敗しました", "meta": meta,
                }

        # -----------------------------------------------------------------
        # P2: LLM 融合結果に framing/location/lighting/color_palette_hex が
        # 抜けていても vision_results 側の該当区間から機械的に補完する
        # （LLM が fusion prompt でこれらを漏らしても shots_ref に到達させる）。
        # -----------------------------------------------------------------
        try:
            enrich_stats = _enrich_shots_ref_from_vision(
                normalized_spec, vision_results, duration_sec,
            )
            meta["shots_ref_enrich"] = enrich_stats
        except Exception as exc:
            warnings.append("shots_ref_enrich 失敗: {}".format(str(exc)[:200]))

        # 3秒ごと storyboard を spec に付与（P1-6）。validate 済み normalized_spec に後付けする
        # （storyboard は任意フィールドで validate はゆるく通す）。
        # storyboard が有効なら結果が空でもキーを付ける（下記 cache 判定を「キーの有無」で
        # 行うため。vision不能等で空になっても毎回再解析しないようにする）。
        if _resolve_storyboard_enabled(cfg):
            normalized_spec["storyboard"] = storyboard_results

        # 統合配線(narration_mode): 解析側で narration 有無を確定して spec に載せる。
        # director はこの spec 値を _infer フォールバックより優先採用する。
        normalized_spec["narration_mode"] = compute_reference_narration_mode(normalized_spec, asr_ok)
        meta["narration_mode"] = normalized_spec["narration_mode"]

        # -----------------------------------------------------------------
        # R2b F2/F8/F7/F6: 決定論的な後段整形（LLM 融合結果を機械観測で補正）
        # -----------------------------------------------------------------
        # F2: 光学フローの camera_move で shots_ref を上書き
        if of_shots:
            shots_ref_updated, of_stats = of_mod.apply_optical_flow_to_shots_ref(
                normalized_spec.get("shots_ref") or [], of_shots,
                override_threshold=float(ref_cfg.get("optical_flow_override_threshold", 0.5)),
            )
            normalized_spec["shots_ref"] = shots_ref_updated
            meta["optical_flow_apply"] = of_stats
            # 空 shots_ref のときは of_shots をそのまま参考shotsとして採用（LLMが漏らしても救う）
            if not (normalized_spec.get("shots_ref") or []):
                normalized_spec["shots_ref"] = [
                    {
                        "start": s.get("start"), "end": s.get("end"),
                        "camera_move": s.get("camera_move"),
                        "camera_move_source": "optical_flow",
                        "intensity": s.get("intensity"),
                        "of_confidence": s.get("confidence"),
                        "motion": s.get("camera_move"),
                    } for s in of_shots
                ]

        # F8: テロップ帯変化検出で telops の start/end を秒精度補正
        telop_refine_enabled = bool(ref_cfg.get("telop_refine_enabled", True))
        if telop_refine_enabled and normalized_spec.get("telops"):
            try:
                change_times = telop_refine_mod.detect_telop_change_times(
                    ffmpeg_bin, video_path,
                    threshold=float(ref_cfg.get("telop_refine_threshold", 0.05)),
                    timeout_sec=int(ref_cfg.get("telop_refine_timeout_sec", 90)),
                )
            except Exception as exc:
                warnings.append("telop_refine 失敗: {}".format(str(exc)[:200]))
                change_times = []
            meta["telop_change_times"] = len(change_times)
            if change_times:
                refined_telops, tel_stats = telop_refine_mod.refine_telops(
                    normalized_spec.get("telops") or [], change_times,
                    tol_sec=float(ref_cfg.get("telop_refine_snap_tol_sec", 0.30)),
                )
                normalized_spec["telops"] = refined_telops
                meta["telop_refine_stats"] = tel_stats

        # F7: ナレーション rhythm 統計
        try:
            rhythm = narration_rhythm_mod.compute_narration_rhythm(
                normalized_spec.get("segments") or [], duration_sec,
                pause_min_sec=float(ref_cfg.get("narration_pause_min_sec", 0.35)),
            )
            normalized_spec["narration_rhythm"] = rhythm
            meta["narration_rhythm"] = {
                "chars_per_sec": rhythm.get("chars_per_sec"),
                "avg_gap_sec": rhythm.get("avg_gap_sec"),
                "pause_points": len(rhythm.get("pause_points") or []),
            }
        except Exception as exc:
            warnings.append("narration_rhythm 失敗: {}".format(str(exc)[:200]))

        # F6: SE 音色 MFCC（参考音色を sfx_events に付与。以後 sfx_planner で参照）
        sfx_matcher_enabled = bool(ref_cfg.get("sfx_matcher_enabled", True))
        if sfx_matcher_enabled and normalized_spec.get("sfx_events"):
            try:
                events_with_mfcc = sfx_matcher_mod.compute_reference_event_mfccs(
                    audio_path, normalized_spec.get("sfx_events") or [],
                    win_sec=float(ref_cfg.get("sfx_matcher_win_sec", 0.20)),
                )
                normalized_spec["sfx_events"] = events_with_mfcc
                nonzero = sum(1 for e in events_with_mfcc if e.get("timbre_mfcc"))
                meta["sfx_events_with_mfcc"] = nonzero
            except Exception as exc:
                warnings.append("sfx_matcher_mfcc 失敗: {}".format(str(exc)[:200]))

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


def _build_cuts_field(cuts: List[float], cut_confidence_map: Optional[Dict[float, float]]) -> List[Dict[str, Any]]:
    """cuts 秒列 + アンサンブル confidence マップ から cuts フィールドを組み立てる。

    マップに無いキーは既定 0.8 を採用（旧単一手法時の互換値）。
    """
    cm = cut_confidence_map or {}
    out = []
    for c in cuts:
        t_r = round(float(c), 3)
        conf = cm.get(c, cm.get(t_r, 0.8))
        out.append({"t": t_r, "confidence": float(conf)})
    return out


def _normalize_v2_from_llm(
    data: Dict[str, Any],
    url: str,
    duration_sec: float,
    asr_and_analysis: Dict[str, Any],
    cuts: List[float],
    onsets: List[Dict[str, Any]],
    warnings: List[str],
    cut_confidence_map: Optional[Dict[float, float]] = None,
    music: Optional[Dict[str, Any]] = None,
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
    # P1 修正: 検出器由来の cuts + confidence を常に採用する（LLM は cuts の秒/信頼度を
    # 正しく再現できず、fusion prompt からのコピペ精度に依存するため）。LLM が cuts を
    # 生成しても、決定的検出結果で上書きする方が下流の QA・beat_snap の一貫性が保たれる。
    if cuts:
        spec["cuts"] = _build_cuts_field(cuts, cut_confidence_map)
    elif not isinstance(spec.get("cuts"), list):
        spec["cuts"] = []
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
    # R2a F5: music を常時付与（LLMがmusic を返しても上書きせず、検出値を優先する。
    # BPM/beat_times は音響解析側が真実源。LLM 応答の music は無視して確実性を担保）。
    if music is not None:
        spec["music"] = {
            "bpm": float(music.get("bpm") or 0.0),
            "beat_times": list(music.get("beat_times") or []),
            "downbeats": list(music.get("downbeats") or []),
            "confidence": float(music.get("confidence") or 0.0),
            "engine": music.get("engine") or "",
        }
    spec["warnings"] = list(spec.get("warnings") or []) + list(warnings or [])
    return spec


def _pick_mode(values: List[str]) -> str:
    """空文字を無視して最頻値を返す。全て空なら ""。"""
    counts: Dict[str, int] = {}
    for v in values:
        if isinstance(v, str) and v.strip():
            k = v.strip()
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _merge_palette_hex(hex_lists: List[List[str]], limit: int = 3) -> List[str]:
    """複数の palette を出現頻度順にマージし、上位 `limit` 色を返す（重複除去・順序=頻度降順）。"""
    counts: Dict[str, int] = {}
    for palette in hex_lists:
        if not isinstance(palette, list):
            continue
        for c in palette:
            if not isinstance(c, str):
                continue
            k = c.strip().lower()
            if len(k) == 7 and k.startswith("#"):
                counts[k] = counts.get(k, 0) + 1
    if not counts:
        return []
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


def _combine_visual_desc_en(existing: str, extras: List[str]) -> str:
    """既存の visual_desc_en が1文以下なら vision_results の説明を追記して 2〜3 文に伸ばす。

    既存が既に十分な情報量（80文字以上）を持っていればそのまま返す。
    """
    base = (existing or "").strip()
    if len(base) >= 80:
        return base
    seen = base
    for extra in extras:
        e = (extra or "").strip()
        if not e or e == base:
            continue
        # 既存と重複する文はスキップ（先頭 30 文字が既存にあれば重複扱い）
        if e[:30] and e[:30] in seen:
            continue
        seen = (seen + " " + e).strip()
        if len(seen) >= 160:
            break
    return seen


def _enrich_shots_ref_from_vision(
    spec: Dict[str, Any],
    vision_results: List[Dict[str, Any]],
    duration_sec: float,
) -> Dict[str, Any]:
    """LLM 融合結果の shots_ref に framing / location / lighting / color_palette_hex を
    vision_results の該当区間から補完し、visual_desc_en が 1 文しかなければ他フレームの
    記述を追記して 2〜3 文に伸ばす。副作用: spec["shots_ref"] を破壊的に更新。

    Returns 統計 dict（テスト・観測用）。
    """
    stats = {"enriched": 0, "palette_added": 0, "desc_extended": 0, "framing_added": 0}
    shots_ref = spec.get("shots_ref") if isinstance(spec, dict) else None
    if not isinstance(shots_ref, list) or not shots_ref:
        return stats
    for shot in shots_ref:
        if not isinstance(shot, dict):
            continue
        s = shot.get("start")
        e = shot.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            continue
        s_f = float(s)
        e_f = float(e)
        # 区間内の vision フレームを拾う（境界一致はより小さい方の shot に含める）
        in_shot = [
            v for v in (vision_results or [])
            if isinstance(v, dict) and isinstance(v.get("time"), (int, float))
            and s_f - 1e-3 <= float(v["time"]) < e_f + 1e-3
        ]
        if not in_shot:
            continue
        enriched_here = False
        if not (shot.get("framing") or "").strip():
            framing = _pick_mode([v.get("framing") for v in in_shot])
            if framing:
                shot["framing"] = framing
                stats["framing_added"] += 1
                enriched_here = True
        if not (shot.get("location") or "").strip():
            location = _pick_mode([v.get("location") for v in in_shot])
            if location:
                shot["location"] = location
                enriched_here = True
        if not (shot.get("lighting") or "").strip():
            lighting = _pick_mode([v.get("lighting") for v in in_shot])
            if lighting:
                shot["lighting"] = lighting
                enriched_here = True
        # codex-review 指摘(P2): LLM が返した palette が非空でも malformed(大文字/0x/無効値)
        # だと下流(mock gradients / higgsfield prompt)が拾えない。まず既存 palette を
        # _normalize_palette_hex で正規化し、正規化後が空なら vision 側 palette を採用する。
        existing_palette = shot.get("color_palette_hex")
        normalized_existing = _normalize_palette_hex(existing_palette) if isinstance(existing_palette, list) else []
        if normalized_existing:
            # 正規化して形が変わった場合はその形を反映（大文字 → 小文字などの上書き）。
            if normalized_existing != existing_palette:
                shot["color_palette_hex"] = normalized_existing
                enriched_here = True
        else:
            palette = _merge_palette_hex([v.get("color_palette_hex") for v in in_shot])
            if palette:
                shot["color_palette_hex"] = palette
                stats["palette_added"] += 1
                enriched_here = True
        # visual_desc_en の補強（既存が1文以下のとき他フレームの記述を追記）
        combined = _combine_visual_desc_en(
            shot.get("visual_desc_en") or "",
            [v.get("visual_desc_en") for v in in_shot],
        )
        if combined and combined != (shot.get("visual_desc_en") or ""):
            shot["visual_desc_en"] = combined
            stats["desc_extended"] += 1
            enriched_here = True
        if enriched_here:
            stats["enriched"] += 1
    return stats


def _correct_v2_spec(
    spec: Dict[str, Any], duration_sec: float, cuts: List[float], onsets: List[Dict[str, Any]],
    cut_confidence_map: Optional[Dict[float, float]] = None,
    music: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """検証失敗時の決定的矯正: cutsは検出結果で上書き、範囲外時刻はクランプ。"""
    corrected = dict(spec)
    corrected["version"] = 2
    corrected["duration_sec"] = float(duration_sec or 0.0)
    corrected["cuts"] = _build_cuts_field(cuts, cut_confidence_map)
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
    if music is not None:
        corrected["music"] = {
            "bpm": float(music.get("bpm") or 0.0),
            "beat_times": list(music.get("beat_times") or []),
            "downbeats": list(music.get("downbeats") or []),
            "confidence": float(music.get("confidence") or 0.0),
            "engine": music.get("engine") or "",
        }
    return corrected
