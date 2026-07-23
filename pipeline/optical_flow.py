# -*- coding: utf-8 -*-
"""R2b F2: 光学フローによるカメラワーク機械判定。

参考動画の各カット区間内でフレーム間 dense optical flow (Farneback) を計算し、
「参考の各shotで支配的なカメラワーク（pan_l / pan_r / tilt_up / tilt_down /
zoom_in / zoom_out / static / handheld）」と強度（weak / strong）を機械的に決定する。

設計方針:
  - Vision(LLM)側の camera_move 推定と対照し、confidence の高い方（機械判定）を優先
    採用する決定論経路。LLM の想像に依存しない。
  - opencv-python-headless の cv2.calcOpticalFlowFarneback を使う。1shotあたり数枚
    (既定 3〜5) を等間隔サンプルし、隣接ペアの平均フローベクトルから pan（横）/
    tilt（縦）/ zoom（発散） を推定する。
  - handheld（三脚無しの手ブレ）は「フロー方向のばらつき（分散）が支配」判定で拾う。

Python 3.9 互換。opencv-python-headless が入っていない環境では `estimate_shots`
が空リストを返す（呼び出し側は空リストなら Vision 側の推定を維持する）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def _try_import_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        return cv2, np
    except Exception:
        return None, None


# 分類しきい値（実測経験値）。1shotの平均フロー(ピクセル/frame)に対する判定。
# ここでの単位は「幅で正規化した割合(0..1)」で扱う。例: mean_dx_ratio = mean_dx / width
_PAN_TH_WEAK = 0.0035   # 0.35% width / frame 以上でpan兆候
_PAN_TH_STRONG = 0.010  # 1.0% width / frame 以上で強pan
_TILT_TH_WEAK = 0.0045  # tilt はやや高めに（垂直の被写体運動と誤検知しやすい）
_TILT_TH_STRONG = 0.012
_ZOOM_TH_WEAK = 0.0009  # divergence(発散) を長さで正規化
_ZOOM_TH_STRONG = 0.0025
_HANDHELD_STD_TH = 0.008  # フロー方向の標準偏差 — 大きいと handheld

# 各shotで解析するペア数（等間隔サンプル）
_DEFAULT_SAMPLES_PER_SHOT = 4
# 1shot が短すぎるとサンプルペアが取れない
_MIN_SHOT_LEN_SEC = 0.4
# フレームリサイズ（速度優先: 240px幅相当まで縮小して Farneback を計算）
_ANALYSIS_WIDTH = 240

# camera_move 語彙（reference_v2.py の vision 出力語彙と揃える）
CAMERA_MOVE_LABELS = (
    "static", "pan_l", "pan_r", "tilt_up", "tilt_down",
    "zoom_in", "zoom_out", "handheld", "dolly",
)


def _read_frame(cv2, cap, frame_idx: int, analysis_w: int):
    """指定 frame_idx にシークして1枚読み、グレースケール+リサイズを返す。"""
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0, frame_idx)))
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None
    scale = float(analysis_w) / float(w)
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (analysis_w, new_h), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    return gray


def _dense_flow_features(cv2, np, prev_gray, next_gray) -> Optional[Dict[str, float]]:
    """Farneback dense flow を計算し、pan/tilt/zoom/handheld 用の特徴量を返す。"""
    try:
        flow = cv2.calcOpticalFlowFarneback(
            prev_gray, next_gray, None,
            pyr_scale=0.5, levels=3, winsize=15, iterations=3,
            poly_n=5, poly_sigma=1.2, flags=0,
        )
    except Exception:
        return None
    if flow is None:
        return None
    h, w = prev_gray.shape[:2]
    # フロー全体の平均ベクトル（幅で正規化）
    mean_dx = float(flow[..., 0].mean())
    mean_dy = float(flow[..., 1].mean())
    mean_dx_ratio = mean_dx / float(w) if w > 0 else 0.0
    mean_dy_ratio = mean_dy / float(h) if h > 0 else 0.0

    # zoom 検出: フローの発散(∇·v)。画面中心からの動径成分の平均で近似する。
    # cx, cy を中心として、各画素の (dx, dy) と (x - cx, y - cy) の内積を長さで正規化した平均。
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0
    rx = xs - cx
    ry = ys - cy
    rlen = np.sqrt(rx * rx + ry * ry) + 1e-6
    radial = (flow[..., 0] * rx + flow[..., 1] * ry) / rlen
    zoom_signal = float(radial.mean())
    # 発散を対角長で正規化
    diag = float((w * w + h * h) ** 0.5)
    zoom_ratio = zoom_signal / diag if diag > 0 else 0.0

    # handheld 判定: フロー方向の標準偏差（大 = ブレ）。画面全体で dx/dy それぞれの分散平均。
    std_dx = float(flow[..., 0].std())
    std_dy = float(flow[..., 1].std())
    std_ratio = (std_dx / float(w) + std_dy / float(h)) / 2.0 if w > 0 and h > 0 else 0.0

    return {
        "mean_dx_ratio": mean_dx_ratio,
        "mean_dy_ratio": mean_dy_ratio,
        "zoom_ratio": zoom_ratio,
        "std_ratio": std_ratio,
    }


def _classify(features: Dict[str, float]) -> Tuple[str, str, float]:
    """特徴量から camera_move ラベル・強度・confidence を返す。

    優先順位: |pan| / |tilt| / |zoom| を絶対値比較し支配的な軸を採る。どれも
    しきい値以下で std_ratio が大きければ handheld、そうでなければ static。
    """
    dx = features.get("mean_dx_ratio") or 0.0
    dy = features.get("mean_dy_ratio") or 0.0
    z = features.get("zoom_ratio") or 0.0
    std = features.get("std_ratio") or 0.0

    abs_dx = abs(dx)
    abs_dy = abs(dy)
    abs_z = abs(z)

    # 支配的な軸を選ぶ（正規化空間で最大のもの）
    axis = max(
        ("pan", abs_dx / _PAN_TH_WEAK if _PAN_TH_WEAK > 0 else 0),
        ("tilt", abs_dy / _TILT_TH_WEAK if _TILT_TH_WEAK > 0 else 0),
        ("zoom", abs_z / _ZOOM_TH_WEAK if _ZOOM_TH_WEAK > 0 else 0),
        key=lambda kv: kv[1],
    )
    axis_name, axis_score = axis

    if axis_name == "pan" and abs_dx >= _PAN_TH_WEAK:
        strong = abs_dx >= _PAN_TH_STRONG
        # 光学フローは「シーン(ピクセル)の見かけの移動」を返す。カメラを右にパンすると
        # シーンは左に流れる → mean_dx<0。逆にカメラを左にパンすると mean_dx>0。
        # したがって dx > 0 は camera pan LEFT、dx < 0 は camera pan RIGHT。
        # (P1 codex-review 修正: 従来は逆向きに符号を割り当てており下流で反対方向の
        # motion_preset が生成されていた)
        move = "pan_l" if dx > 0 else "pan_r"
        conf = min(1.0, abs_dx / (_PAN_TH_STRONG * 2))
        return move, ("strong" if strong else "weak"), round(conf, 3)
    if axis_name == "tilt" and abs_dy >= _TILT_TH_WEAK:
        strong = abs_dy >= _TILT_TH_STRONG
        # 画像座標系は下向きが正 → カメラを上に tilt するとシーンが下に流れ mean_dy>0、
        # カメラを下に tilt するとシーンが上に流れ mean_dy<0。dy > 0 = tilt_up。
        move = "tilt_up" if dy > 0 else "tilt_down"
        conf = min(1.0, abs_dy / (_TILT_TH_STRONG * 2))
        return move, ("strong" if strong else "weak"), round(conf, 3)
    if axis_name == "zoom" and abs_z >= _ZOOM_TH_WEAK:
        strong = abs_z >= _ZOOM_TH_STRONG
        # 発散>0 は画像が中心から外向きに拡張する → ピクセルが視野の外へ流れ出る
        # = カメラが被写体に近づく or FOV が狭くなる = zoom_IN。発散<0 は zoom_out。
        # (P1 codex-review 修正: 従来は逆向きに符号を割り当てていた)
        move = "zoom_in" if z > 0 else "zoom_out"
        conf = min(1.0, abs_z / (_ZOOM_TH_STRONG * 2))
        return move, ("strong" if strong else "weak"), round(conf, 3)

    if std >= _HANDHELD_STD_TH:
        # handheld: 支配軸弱いがフローの散らばりが大きい
        conf = min(1.0, std / (_HANDHELD_STD_TH * 2))
        return "handheld", "weak", round(conf, 3)

    return "static", "weak", 0.6  # フローがほぼ無い = static (中程度の confidence)


def estimate_shots(
    video_path: str,
    cuts: List[float],
    duration_sec: float,
    samples_per_shot: int = _DEFAULT_SAMPLES_PER_SHOT,
    analysis_width: int = _ANALYSIS_WIDTH,
) -> List[Dict[str, Any]]:
    """各カット区間(shot)ごとに camera_move を機械判定する。

    Args:
        video_path: ローカル mp4 パス。
        cuts: 内部カット秒列（先頭 0 と末尾 duration は含まない）。
        duration_sec: 動画総尺（秒）。
        samples_per_shot: 各shotで解析するサンプルフレーム数（隣接ペアで flow 計算）。
        analysis_width: 縮小後の解析幅（速度優先）。

    Returns:
        list[dict]: [{"start", "end", "camera_move", "intensity", "confidence",
                     "features": {mean_dx_ratio, mean_dy_ratio, zoom_ratio, std_ratio},
                     "engine": "optical_flow_farneback"}]
        opencv 未導入・video 読めない場合は空リストを返す（呼び出し側は fallback）。
    """
    cv2, np = _try_import_cv2()
    if cv2 is None or np is None:
        return []
    if not video_path or not os.path.exists(video_path):
        return []
    if not duration_sec or duration_sec <= 0:
        return []

    # 区間境界
    boundaries = [0.0] + [float(c) for c in (cuts or []) if 0 < float(c) < float(duration_sec)] + [float(duration_sec)]
    # 重複除去+単調
    dedup = []
    for b in sorted(boundaries):
        if not dedup or b > dedup[-1] + 1e-3:
            dedup.append(b)
    if len(dedup) < 2:
        return []

    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return []
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if fps <= 0:
            fps = 30.0
    except Exception:
        return []

    results: List[Dict[str, Any]] = []
    try:
        for i in range(len(dedup) - 1):
            seg_start = dedup[i]
            seg_end = dedup[i + 1]
            seg_len = seg_end - seg_start
            if seg_len < _MIN_SHOT_LEN_SEC:
                # 短すぎる区間は 1ペアだけ試みる（両端）
                sample_times = [seg_start + 0.05, max(seg_start + 0.1, seg_end - 0.05)]
            else:
                # 内側にサンプル点を等間隔で
                n = max(2, int(samples_per_shot))
                step = seg_len / float(n + 1)
                sample_times = [seg_start + step * (k + 1) for k in range(n)]

            grays = []
            for t in sample_times:
                frame_idx = int(round(t * fps))
                g = _read_frame(cv2, cap, frame_idx, analysis_width)
                if g is not None:
                    grays.append(g)
            if len(grays) < 2:
                results.append({
                    "start": round(seg_start, 3), "end": round(seg_end, 3),
                    "camera_move": "static", "intensity": "weak", "confidence": 0.3,
                    "features": {}, "engine": "optical_flow_farneback",
                    "note": "frames_unavailable",
                })
                continue
            feats: List[Dict[str, float]] = []
            for k in range(len(grays) - 1):
                f = _dense_flow_features(cv2, np, grays[k], grays[k + 1])
                if f is not None:
                    feats.append(f)
            if not feats:
                results.append({
                    "start": round(seg_start, 3), "end": round(seg_end, 3),
                    "camera_move": "static", "intensity": "weak", "confidence": 0.3,
                    "features": {}, "engine": "optical_flow_farneback",
                    "note": "flow_failed",
                })
                continue
            # 平均特徴量
            agg = {
                "mean_dx_ratio": sum(f["mean_dx_ratio"] for f in feats) / len(feats),
                "mean_dy_ratio": sum(f["mean_dy_ratio"] for f in feats) / len(feats),
                "zoom_ratio": sum(f["zoom_ratio"] for f in feats) / len(feats),
                "std_ratio": sum(f["std_ratio"] for f in feats) / len(feats),
            }
            move, intensity, conf = _classify(agg)
            results.append({
                "start": round(seg_start, 3), "end": round(seg_end, 3),
                "camera_move": move, "intensity": intensity, "confidence": conf,
                "features": {k: round(float(v), 5) for k, v in agg.items()},
                "engine": "optical_flow_farneback",
            })
    finally:
        try:
            cap.release()
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# camera_move -> plan.motion_preset 決定論マッピング（F4 と共通）
# ---------------------------------------------------------------------------

# plan_schema.MOTION_PRESETS = ("static", "pan_left", "pan_right",
#                                "zoom_in", "zoom_out", "ken_burns")
# camera_move ラベルは v1 と F3 拡張の両方を吸収する。
_CAMERA_MOVE_TO_PRESET = {
    "static": "static",
    "none": "static",
    "person_talking": "static",
    "cut_transition": "static",
    "pan_left": "pan_left", "pan_l": "pan_left",
    "pan_right": "pan_right", "pan_r": "pan_right",
    "zoom_in": "zoom_in",
    "zoom_out": "zoom_out",
    # tilt/handheld/dolly は既存の 6preset では表現できないので ken_burns にまとめる。
    # motion_preset には強度を持たないため、mock_backend/higgsfield_backend 側で
    # intensity を読み取って強弱を反映する（強度は reference_visual に別途保持）。
    "tilt_up": "ken_burns",
    "tilt_down": "ken_burns",
    "handheld": "ken_burns",
    "dolly": "ken_burns",
}


def camera_move_to_preset(camera_move: Optional[str]) -> str:
    """camera_move ラベル → plan.motion_preset。未知/空は static。"""
    if not isinstance(camera_move, str) or not camera_move:
        return "static"
    return _CAMERA_MOVE_TO_PRESET.get(camera_move.strip().lower(), "static")


def apply_optical_flow_to_shots_ref(
    shots_ref: List[Dict[str, Any]],
    of_shots: List[Dict[str, Any]],
    override_threshold: float = 0.5,
    overlap_tol_sec: float = 0.15,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """spec.shots_ref に光学フロー由来 camera_move / intensity を反映する。

    - shots_ref の (start, end) と of_shots の (start, end) を突き合わせ、
      「区間が主に重なる」ペアを紐付ける。
    - of_shot.confidence >= override_threshold のとき shots_ref[i].camera_move を上書きし、
      shots_ref[i].camera_move_source = "optical_flow"、intensity と of_confidence も付ける。
    - override_threshold 未満のときは shots_ref[i].camera_move はそのままにし、
      shots_ref[i].camera_move_of / intensity_of / of_confidence を「参考情報」として付ける。
    - shots_ref に camera_move が無い場合は confidence にかかわらず OF値を採用する。

    Returns:
        (new_shots_ref, stats)  stats={"overridden": int, "kept": int, "added": int}
    """
    stats = {"overridden": 0, "kept": 0, "added": 0}
    if not shots_ref or not of_shots:
        return list(shots_ref or []), stats
    new_shots: List[Dict[str, Any]] = []
    for sr in shots_ref:
        if not isinstance(sr, dict):
            new_shots.append(sr)
            continue
        ss = sr.get("start")
        se = sr.get("end")
        if not isinstance(ss, (int, float)) or not isinstance(se, (int, float)):
            new_shots.append(sr)
            continue
        # 最も重なる of_shot を選ぶ
        best = None
        best_overlap = 0.0
        for of in of_shots:
            os_s = of.get("start")
            os_e = of.get("end")
            if not isinstance(os_s, (int, float)) or not isinstance(os_e, (int, float)):
                continue
            lo = max(float(ss), float(os_s))
            hi = min(float(se), float(os_e))
            ov = max(0.0, hi - lo)
            if ov > best_overlap + 1e-6:
                best_overlap = ov
                best = of
        merged = dict(sr)
        if best is not None and best_overlap >= overlap_tol_sec:
            of_move = best.get("camera_move") or ""
            of_conf = float(best.get("confidence") or 0.0)
            existing = (sr.get("camera_move") or "").strip().lower()
            if not existing:
                merged["camera_move"] = of_move
                merged["camera_move_source"] = "optical_flow"
                merged["intensity"] = best.get("intensity")
                merged["of_confidence"] = of_conf
                stats["added"] += 1
            elif of_conf >= override_threshold:
                # 機械判定を優先。既存の LLM 推定を _vision に退避しておく（後追い検証用）。
                merged["camera_move_vision"] = existing
                merged["camera_move"] = of_move
                merged["camera_move_source"] = "optical_flow"
                merged["intensity"] = best.get("intensity")
                merged["of_confidence"] = of_conf
                stats["overridden"] += 1
            else:
                # LLM推定を尊重しつつ、機械判定を参考情報として添付
                merged.setdefault("camera_move_source", "vision")
                merged["camera_move_of"] = of_move
                merged["intensity_of"] = best.get("intensity")
                merged["of_confidence"] = of_conf
                stats["kept"] += 1
        new_shots.append(merged)
    return new_shots, stats
