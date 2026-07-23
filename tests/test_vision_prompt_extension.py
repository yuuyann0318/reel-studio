# -*- coding: utf-8 -*-
"""F3: reference_vision_prompt.txt に構造化フィールド (shot_size / subject_count /
camera_move / color_mood) が追加され、reference_v2 側の vision 結果統合で保持されることの
単体テスト。
"""
from __future__ import annotations

import os
from pathlib import Path

from pipeline import reference_v2


_PROMPT_PATH = Path(reference_v2.__file__).parent / "prompts" / "reference_vision_prompt.txt"


def test_vision_prompt_includes_f3_extension_fields():
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    for field in ("shot_size", "subject_count", "camera_move", "color_mood"):
        assert field in text, "reference_vision_prompt.txt に {} フィールドが追加されていない".format(field)
    # 構図語彙・カメラワーク語彙が明示されている
    assert "closeup" in text and "medium" in text and "wide" in text
    assert "pan_l" in text and "pan_r" in text and "zoom_in" in text and "zoom_out" in text


def test_reference_v2_run_vision_preserves_extension_fields(monkeypatch, tmp_path):
    """vision バッチ呼び出しが返す item に F3 フィールドが載っていれば results にも保存される。"""
    # モックの vision_call: 期待スキーマの JSON 配列を返す
    def _fake_call(prompt, paths, timeout_sec=None):
        return {
            "ok": True,
            "data": [{
                "index": 1,
                "path": paths[0],
                "telop_text": "テスト",
                "telop_position": "top",
                "telop_color": "yellow",
                "telop_stroke": "black stroke",
                "emphasis_words": ["テスト"],
                "size_class": "large",
                "visual_desc_en": "Aerial view of a Minecraft frog pond.",
                "motion": "static",
                "has_person": False,
                "has_product_logo": False,
                "shot_size": "wide",
                "subject_count": 3,
                "camera_move": "zoom_in",
                "color_mood": "vivid",
            }],
        }

    frames = [{"index": 1, "time": 0.5, "path": str(tmp_path / "f1.png")}]
    Path(frames[0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\n")
    results, warnings = reference_v2.analyze_frames_with_vision(
        frames, cfg={"reference": {"vision_batch_size": 1, "max_vision_calls": 1, "vision_timeout_sec": 10}},
        vision_call=_fake_call,
    )
    assert len(results) == 1
    r = results[0]
    assert r["shot_size"] == "wide"
    assert r["subject_count"] == 3
    assert r["camera_move"] == "zoom_in"
    assert r["color_mood"] == "vivid"


def test_reference_v2_run_vision_defaults_when_extension_fields_missing(monkeypatch, tmp_path):
    """旧スキーマ（F3 フィールド無し）の応答でも既定値で埋めて壊れない。"""
    def _fake_call(prompt, paths, timeout_sec=None):
        return {
            "ok": True,
            "data": [{
                "index": 1,
                "path": paths[0],
                "telop_text": "テスト",
                "telop_position": "top",
                "telop_color": "yellow",
                "telop_stroke": "black stroke",
                "emphasis_words": [],
                "size_class": "large",
                "visual_desc_en": "old schema no F3",
                "motion": "static",
                "has_person": False,
                "has_product_logo": False,
                # F3 拡張フィールドを敢えて欠落させる
            }],
        }
    frames = [{"index": 1, "time": 0.5, "path": str(tmp_path / "f1.png")}]
    Path(frames[0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\n")
    results, _ = reference_v2.analyze_frames_with_vision(
        frames, cfg={"reference": {"vision_batch_size": 1, "max_vision_calls": 1, "vision_timeout_sec": 10}},
        vision_call=_fake_call,
    )
    r = results[0]
    assert r["shot_size"] == ""
    assert r["subject_count"] == 0
    assert r["camera_move"] == ""
    assert r["color_mood"] == ""


def test_map_reference_visual_reads_extension_fields():
    """director._map_reference_visual_to_shots が shots_ref の F3 拡張を skeleton に運ぶ。"""
    from pipeline import director
    shots_ref = [{
        "index": 0, "start": 0.0, "end": 3.0,
        "visual_desc_en": "kitchen close-up",
        "motion": "static", "shot_size": "closeup", "subject_count": 2,
        "camera_move": "zoom_in", "color_mood": "warm",
    }]
    rv = director._map_reference_visual_to_shots(shots_ref, [0.0, 3.0], ["s1"])
    assert "s1" in rv
    r = rv["s1"]
    assert r["desc_en"] == "kitchen close-up"
    assert r["shot_size"] == "closeup"
    assert r["subject_count"] == 2
    assert r["camera_move"] == "zoom_in"
    assert r["color_mood"] == "warm"
