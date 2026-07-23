# -*- coding: utf-8 -*-
"""R2b F2/F4: 光学フローによる camera_move 判定と motion_preset 写像のテスト。"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline import optical_flow


class TestClassify:
    def test_static_when_all_zero(self):
        feats = {"mean_dx_ratio": 0.0, "mean_dy_ratio": 0.0, "zoom_ratio": 0.0, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "static"
        assert intensity == "weak"

    def test_pan_right_when_dx_negative(self):
        # dx < 0 = ピクセルが左に流れる = カメラは右にパン
        feats = {"mean_dx_ratio": -0.006, "mean_dy_ratio": 0.0, "zoom_ratio": 0.0, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "pan_r"
        assert intensity == "weak"

    def test_pan_left_strong_when_dx_positive(self):
        # dx > 0 = ピクセルが右に流れる = カメラは左にパン（強度: strong）
        feats = {"mean_dx_ratio": 0.02, "mean_dy_ratio": 0.0, "zoom_ratio": 0.0, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "pan_l"
        assert intensity == "strong"

    def test_zoom_out_when_radial_negative(self):
        # 発散 < 0 = 中心に集束 = zoom_out
        feats = {"mean_dx_ratio": 0.0, "mean_dy_ratio": 0.0, "zoom_ratio": -0.002, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "zoom_out"

    def test_zoom_in_when_radial_positive_strong(self):
        # 発散 > 0 = 外向きに拡張 = zoom_in
        feats = {"mean_dx_ratio": 0.0, "mean_dy_ratio": 0.0, "zoom_ratio": 0.005, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "zoom_in"
        assert intensity == "strong"

    def test_tilt_up_when_dy_positive(self):
        feats = {"mean_dx_ratio": 0.0, "mean_dy_ratio": 0.008, "zoom_ratio": 0.0, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "tilt_up"

    def test_handheld_when_std_large(self):
        feats = {"mean_dx_ratio": 0.001, "mean_dy_ratio": 0.001, "zoom_ratio": 0.0, "std_ratio": 0.02}
        move, intensity, conf = optical_flow._classify(feats)
        assert move == "handheld"

    def test_axis_priority_pan_dominates_over_weak_zoom(self):
        # pan が支配的な場合は zoom を選ばない（dx>0 なので pan_l）
        feats = {"mean_dx_ratio": 0.02, "mean_dy_ratio": 0.0, "zoom_ratio": 0.001, "std_ratio": 0.0}
        move, intensity, conf = optical_flow._classify(feats)
        assert move in ("pan_l",)


class TestCameraMoveToPreset:
    def test_maps_pan_left(self):
        assert optical_flow.camera_move_to_preset("pan_l") == "pan_left"
        assert optical_flow.camera_move_to_preset("pan_left") == "pan_left"

    def test_maps_zoom_in_out(self):
        assert optical_flow.camera_move_to_preset("zoom_in") == "zoom_in"
        assert optical_flow.camera_move_to_preset("zoom_out") == "zoom_out"

    def test_tilt_and_handheld_fallback_to_ken_burns(self):
        assert optical_flow.camera_move_to_preset("tilt_up") == "ken_burns"
        assert optical_flow.camera_move_to_preset("handheld") == "ken_burns"

    def test_unknown_or_empty_returns_static(self):
        assert optical_flow.camera_move_to_preset("") == "static"
        assert optical_flow.camera_move_to_preset(None) == "static"
        assert optical_flow.camera_move_to_preset("weird_thing") == "static"


class TestApplyOpticalFlowToShotsRef:
    def test_overrides_when_confidence_above_threshold(self):
        shots_ref = [{"start": 0.0, "end": 2.0, "camera_move": "static", "motion": "static"}]
        of_shots = [{"start": 0.0, "end": 2.0, "camera_move": "pan_l",
                     "intensity": "strong", "confidence": 0.9}]
        new_shots, stats = optical_flow.apply_optical_flow_to_shots_ref(
            shots_ref, of_shots, override_threshold=0.5,
        )
        assert new_shots[0]["camera_move"] == "pan_l"
        assert new_shots[0]["camera_move_source"] == "optical_flow"
        assert new_shots[0]["intensity"] == "strong"
        assert new_shots[0]["camera_move_vision"] == "static"
        assert stats["overridden"] == 1

    def test_keeps_vision_when_confidence_below_threshold(self):
        shots_ref = [{"start": 0.0, "end": 2.0, "camera_move": "zoom_in"}]
        of_shots = [{"start": 0.0, "end": 2.0, "camera_move": "pan_l",
                     "intensity": "weak", "confidence": 0.3}]
        new_shots, stats = optical_flow.apply_optical_flow_to_shots_ref(
            shots_ref, of_shots, override_threshold=0.5,
        )
        assert new_shots[0]["camera_move"] == "zoom_in"  # 維持
        assert new_shots[0]["camera_move_of"] == "pan_l"  # 参考情報として付与
        assert stats["kept"] == 1
        assert stats["overridden"] == 0

    def test_adds_when_vision_empty(self):
        shots_ref = [{"start": 0.0, "end": 2.0}]  # camera_move 無し
        of_shots = [{"start": 0.0, "end": 2.0, "camera_move": "pan_r",
                     "intensity": "weak", "confidence": 0.4}]
        new_shots, stats = optical_flow.apply_optical_flow_to_shots_ref(
            shots_ref, of_shots, override_threshold=0.5,
        )
        assert new_shots[0]["camera_move"] == "pan_r"
        assert new_shots[0]["camera_move_source"] == "optical_flow"
        assert stats["added"] == 1

    def test_no_overlap_leaves_untouched(self):
        shots_ref = [{"start": 0.0, "end": 2.0, "camera_move": "static"}]
        of_shots = [{"start": 5.0, "end": 7.0, "camera_move": "pan_l", "confidence": 0.9}]
        new_shots, stats = optical_flow.apply_optical_flow_to_shots_ref(
            shots_ref, of_shots, override_threshold=0.5,
        )
        assert new_shots[0]["camera_move"] == "static"
        assert stats == {"overridden": 0, "kept": 0, "added": 0}


class TestEstimateShotsIntegrationSmoke:
    """cv2 が入っている環境でだけ estimate_shots が動くことを軽く検証。"""

    def test_returns_empty_for_missing_video(self):
        # 存在しない mp4 は空リスト
        res = optical_flow.estimate_shots("/nonexistent/path.mp4", cuts=[], duration_sec=5.0)
        assert res == []

    def test_returns_empty_when_duration_zero(self):
        res = optical_flow.estimate_shots("whatever.mp4", cuts=[], duration_sec=0)
        assert res == []
