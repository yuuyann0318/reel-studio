# -*- coding: utf-8 -*-
"""P2 KR3: fidelity.compute_product_subordination — 商品モードが参考の骨格
（cut / camera / beat）を歪めていないかを判定する。
"""
from __future__ import annotations

import json
import os
import tempfile

from qa import fidelity


def _make_summary(cut_match=1.0, telop_iou=0.5, telop_style=0.5,
                  sfx_placement=0.5, camera_move=1.0, beat_alignment=0.9):
    return {
        "summary": {
            "cut_match": cut_match,
            "telop_iou": telop_iou,
            "telop_style": telop_style,
            "sfx_placement": sfx_placement,
            "camera_move": camera_move,
            "beat_alignment": beat_alignment,
        }
    }


def test_subordination_ok_when_skeleton_metrics_identical():
    """商品ありplanと商品なしplanで cut/camera/beat が同じなら subordination_ok=True。"""
    fid_no = _make_summary()
    fid_with = _make_summary()
    result = fidelity.compute_product_subordination(fid_no, fid_with)
    assert result["subordination_ok"] is True
    assert result["diffs"]["cut_match"]["delta"] == 0.0
    assert result["diffs"]["camera_move"]["delta"] == 0.0
    assert result["diffs"]["beat_alignment"]["delta"] == 0.0
    assert not result["unmeasurable"]


def test_subordination_ok_when_within_tolerance():
    """差が 0.1 以内なら OK。telop 系は骨格ではないので判定に影響しない。"""
    fid_no = _make_summary(cut_match=0.9, camera_move=0.8, beat_alignment=0.7,
                           telop_iou=0.5, telop_style=0.4)
    fid_with = _make_summary(cut_match=0.95, camera_move=0.75, beat_alignment=0.78,
                             telop_iou=0.1, telop_style=0.1)
    result = fidelity.compute_product_subordination(fid_no, fid_with, tolerance=0.1)
    assert result["subordination_ok"] is True
    # telop 系は診断対象外
    assert "telop_iou" not in result["diffs"]
    assert "telop_style" not in result["diffs"]


def test_subordination_flags_when_camera_or_cut_diverges():
    """cut_match が 0.4 も乖離したら subordination_ok=False。"""
    fid_no = _make_summary(cut_match=0.9, camera_move=0.8, beat_alignment=0.8)
    fid_with = _make_summary(cut_match=0.5, camera_move=0.8, beat_alignment=0.8)
    result = fidelity.compute_product_subordination(fid_no, fid_with)
    assert result["subordination_ok"] is False
    assert result["diffs"]["cut_match"]["within"] is False
    assert result["diffs"]["cut_match"]["delta"] > 0.1


def test_subordination_handles_unmeasurable_beat():
    """beat_alignment が None（BGM 無し等）ならその指標は unmeasurable として除外。"""
    fid_no = _make_summary(beat_alignment=None)
    fid_with = _make_summary(beat_alignment=None)
    result = fidelity.compute_product_subordination(fid_no, fid_with)
    assert "beat_alignment" in result["unmeasurable"]
    assert result["subordination_ok"] is True  # cut/camera は同一なので OK


def test_cli_computes_product_subordination(tmp_path):
    """--plan-no-product を渡すと結果 JSON に product_subordination が入る。"""
    # 最小限の参考 spec と 2 plan を用意
    spec = {
        "version": 2, "url": "u", "duration_sec": 10.0,
        "transcript": "", "segments": [], "beats": [], "rhythm": None,
        "cuts": [{"t": 5.0, "confidence": 0.9}],
        "shots_ref": [
            {"index": 0, "start": 0.0, "end": 5.0, "visual_desc_en": "A", "motion": "static",
             "camera_move": "zoom_in"},
            {"index": 1, "start": 5.0, "end": 10.0, "visual_desc_en": "B", "motion": "static",
             "camera_move": "pan_l"},
        ],
        "telops": [], "sfx_events": [],
        "bgm": {"present": False, "mood_guess": ""},
        "warnings": [],
    }
    plan_common_shots = [
        {"id": "s1", "duration_sec": 5.0, "motion_preset": "zoom_in", "caption_jp": ""},
        {"id": "s2", "duration_sec": 5.0, "motion_preset": "pan_left", "caption_jp": ""},
    ]
    plan_no = {"shots": plan_common_shots, "sfx_plan": []}
    # 商品あり: 骨格は同じ、shot に image_path を注入したことを想定して plan 構造は同一
    plan_with = {"shots": plan_common_shots, "sfx_plan": []}

    p_spec = tmp_path / "spec.json"; p_spec.write_text(json.dumps(spec), encoding="utf-8")
    p_no = tmp_path / "plan_no.json"; p_no.write_text(json.dumps(plan_no), encoding="utf-8")
    p_with = tmp_path / "plan_with.json"; p_with.write_text(json.dumps(plan_with), encoding="utf-8")
    p_out = tmp_path / "out.json"
    rc = fidelity.main([
        "--reference-spec", str(p_spec),
        "--plan", str(p_with),
        "--plan-no-product", str(p_no),
        "--out", str(p_out),
    ])
    assert rc == 0
    result = json.loads(p_out.read_text(encoding="utf-8"))
    assert "product_subordination" in result
    assert result["product_subordination"]["subordination_ok"] is True
    assert "no_product_summary" in result
