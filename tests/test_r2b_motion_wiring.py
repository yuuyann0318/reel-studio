# -*- coding: utf-8 -*-
"""R2b F4: motion_preset がスケルトンに機械写像される + higgsfield/mock の反映テスト。"""
from __future__ import annotations

from pipeline import director
from pipeline.visual import higgsfield_backend
from pipeline.visual import mock_backend


class TestBuildShotSkeletonMotionPreset:
    def test_pan_left_camera_move_becomes_pan_left_preset(self):
        spec = {
            "duration_sec": 4.0,
            "cuts": [2.0],
            "shots_ref": [
                {"start": 0.0, "end": 2.0, "camera_move": "pan_l", "motion": "pan_l",
                 "intensity": "strong"},
                {"start": 2.0, "end": 4.0, "camera_move": "zoom_in", "motion": "zoom_in",
                 "intensity": "weak"},
            ],
            "telops": [], "sfx_events": [],
        }
        skel = director.build_shot_skeleton(spec, target_duration_sec=4.0, min_shot_sec=1.5)
        # skeleton には shot ごとに motion_preset が入る
        presets = [s.get("motion_preset") for s in skel["shots"]]
        assert presets[0] == "pan_left"
        assert presets[1] == "zoom_in"
        # intensity も反映（強度で mock zoompan を変える）
        intensities = [s.get("motion_intensity") for s in skel["shots"]]
        assert intensities[0] == "strong"
        assert intensities[1] == "weak"

    def test_tilt_up_maps_to_ken_burns(self):
        spec = {
            "duration_sec": 3.0,
            "cuts": [],
            "shots_ref": [{"start": 0.0, "end": 3.0, "camera_move": "tilt_up", "motion": "tilt_up"}],
            "telops": [], "sfx_events": [],
        }
        skel = director.build_shot_skeleton(spec, target_duration_sec=3.0, min_shot_sec=1.5)
        assert skel["shots"][0]["motion_preset"] == "ken_burns"

    def test_static_by_default_when_no_shots_ref(self):
        spec = {
            "duration_sec": 4.0,
            "cuts": [2.0],
            "shots_ref": [],
            "telops": [], "sfx_events": [],
        }
        skel = director.build_shot_skeleton(spec, target_duration_sec=4.0, min_shot_sec=1.5)
        assert skel["shots"][0]["motion_preset"] == "static"
        assert skel["shots"][1]["motion_preset"] == "static"


class TestValidateSkeletonMotionPreset:
    def test_mismatch_flags_error(self):
        skel = {
            "shots": [
                {"id": "s1", "duration_sec": 2.0, "motion_preset": "pan_left"},
                {"id": "s2", "duration_sec": 2.0, "motion_preset": "zoom_in"},
            ],
            "sfx_plan": [],
            "hook_end_shot_id": None,
            "cta_start_shot_id": None,
        }
        # plan が異なる motion_preset を返した
        plan = {
            "shots": [
                {"id": "s1", "duration_sec": 2.0, "motion_preset": "static"},
                {"id": "s2", "duration_sec": 2.0, "motion_preset": "zoom_in"},
            ],
        }
        errs = director._validate_plan_matches_skeleton(plan, skel, target_duration_sec=4.0)
        joined = "\n".join(errs)
        assert "motion_preset" in joined
        assert "static" in joined

    def test_match_ok(self):
        skel = {
            "shots": [
                {"id": "s1", "duration_sec": 2.0, "motion_preset": "pan_left"},
            ],
            "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
        }
        plan = {
            "shots": [
                {"id": "s1", "duration_sec": 2.0, "motion_preset": "pan_left"},
            ],
        }
        errs = director._validate_plan_matches_skeleton(plan, skel, target_duration_sec=2.0)
        assert not any("motion_preset" in e for e in errs)


class TestHiggsfieldPromptAugmentation:
    def test_pan_left_added_to_prompt(self):
        shot = {
            "id": "s1",
            "visual_prompt": "a girl in a park",
            "duration_sec": 5,
            "reference_visual": {"camera_move": "pan_l", "shot_size": "medium",
                                 "color_mood": "warm"},
        }
        cmd = higgsfield_backend._build_create_cmd(
            "higgsfield", "seedance_2_0_mini", shot, "480p"
        )
        prompt = cmd[cmd.index("--prompt") + 1]
        assert "camera pans to the left" in prompt
        assert "medium shot composition" in prompt
        assert "warm color mood" in prompt

    def test_zoom_in_strong(self):
        shot = {
            "id": "s1", "visual_prompt": "abstract",
            "duration_sec": 5,
            "reference_visual": {"camera_move": "zoom_in", "intensity": "strong"},
        }
        cmd = higgsfield_backend._build_create_cmd(
            "higgsfield", "seedance_2_0_mini", shot, "480p"
        )
        prompt = cmd[cmd.index("--prompt") + 1]
        assert "camera zooms in" in prompt or "camera slowly zooms in" in prompt
        assert "strong" in prompt

    def test_no_reference_visual_leaves_prompt_untouched(self):
        shot = {"id": "s1", "visual_prompt": "abstract", "duration_sec": 5}
        cmd = higgsfield_backend._build_create_cmd(
            "higgsfield", "seedance_2_0_mini", shot, "480p"
        )
        prompt = cmd[cmd.index("--prompt") + 1]
        assert prompt == "abstract"

    def test_does_not_duplicate_camera_phrase(self):
        # 既に camera pan が prompt にある場合はカメラ句を追加しない（重複防止）
        shot = {
            "id": "s1",
            "visual_prompt": "a shot where the camera pans slowly across a room",
            "duration_sec": 5,
            "reference_visual": {"camera_move": "pan_l", "color_mood": "warm"},
        }
        cmd = higgsfield_backend._build_create_cmd(
            "higgsfield", "seedance_2_0_mini", shot, "480p"
        )
        prompt = cmd[cmd.index("--prompt") + 1]
        # camera pan は元プロンプトのみ（追加句は入っていない）
        assert prompt.lower().count("camera pans") == 1
        # 構図/色調は追記される
        assert "warm color mood" in prompt


class TestMockMotionIntensity:
    def test_zoom_in_strong_uses_larger_delta(self):
        # intensity=strong は zoom delta 0.45（0.3 × 1.5）
        z1, _, _ = mock_backend._motion_exprs("zoom_in", 150, intensity="strong")
        z2, _, _ = mock_backend._motion_exprs("zoom_in", 150, intensity="weak")
        z3, _, _ = mock_backend._motion_exprs("zoom_in", 150, intensity=None)
        # weak < default < strong の順で zoom 変化量が増える
        assert "0.195" in z2 or "0.2" in z2  # 0.3 × 0.65 = 0.195
        assert "0.3" in z3
        assert "0.45" in z1
