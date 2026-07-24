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


# ---------------------------------------------------------------------------
# P2: framing / location / lighting / color_palette_hex を vision→shots_ref→director
# まで貫通させる（KR1）
# ---------------------------------------------------------------------------

def test_vision_prompt_includes_p2_extension_fields():
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    for field in ("framing", "location", "lighting", "color_palette_hex"):
        assert field in text, "reference_vision_prompt.txt に {} フィールドが追加されていない".format(field)
    # 主要な framing 語彙が明示されていること
    for token in ("rule_of_thirds", "product_on_hand", "aerial"):
        assert token in text
    # visual_desc_en は 2〜3 文の指示があること
    assert "2〜3文" in text or "2-3 sentences" in text


def test_reference_v2_run_vision_preserves_p2_fields(tmp_path):
    def _fake_call(prompt, paths, timeout_sec=None):
        return {
            "ok": True,
            "data": [{
                "index": 1,
                "path": paths[0],
                "telop_text": "",
                "telop_position": "none",
                "telop_color": "",
                "telop_stroke": "",
                "emphasis_words": [],
                "size_class": "",
                "visual_desc_en": "Closeup of hands squeezing serum. Bathroom counter behind. Static handheld.",
                "motion": "static",
                "has_person": True,
                "has_product_logo": False,
                "shot_size": "closeup",
                "subject_count": 1,
                "camera_move": "none",
                "color_mood": "warm",
                "framing": "product_on_hand",
                "location": "bathroom",
                "lighting": "soft daylight",
                "color_palette_hex": ["#F4E6D0", "#3b2a1a"],
            }],
        }

    frames = [{"index": 1, "time": 0.5, "path": str(tmp_path / "f1.png")}]
    Path(frames[0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\n")
    results, _ = reference_v2.analyze_frames_with_vision(
        frames, cfg={"reference": {"vision_batch_size": 1, "max_vision_calls": 1, "vision_timeout_sec": 10}},
        vision_call=_fake_call,
    )
    r = results[0]
    assert r["framing"] == "product_on_hand"
    assert r["location"] == "bathroom"
    assert r["lighting"] == "soft daylight"
    # 小文字化・"#" 前置きが保証されること
    assert r["color_palette_hex"] == ["#f4e6d0", "#3b2a1a"]


def test_normalize_palette_hex_variants():
    from pipeline import reference_v2 as rv2
    assert rv2._normalize_palette_hex(["#f4e6d0", "3b2a1a", "0xFFFFFF"]) == ["#f4e6d0", "#3b2a1a", "#ffffff"]
    # 重複除去・4 色目以降を捨てる
    assert rv2._normalize_palette_hex(["#000000", "#000000", "#111111", "#222222", "#333333"]) == [
        "#000000", "#111111", "#222222",
    ]
    # 無効値を捨てる
    assert rv2._normalize_palette_hex(["red", "#12", None, 42, "#gghhii"]) == []
    assert rv2._normalize_palette_hex(None) == []


def test_select_frame_times_from_cuts_frames_per_shot_3():
    """supreme_plus 想定: frames_per_shot=3 で各カット区間の頭/中央/末尾から抽出する。"""
    times = reference_v2.select_frame_times_from_cuts(
        cuts=[2.0, 6.0], duration_sec=10.0, max_frames=40, frames_per_shot=3,
    )
    # 区間 [0,2], [2,6], [6,10] → 3 * 3 = 9 枚（各区間 head/mid/tail、微短区間は 1〜2 に減る）
    # ここでは全区間が 2s 以上あるので 3 枚ずつ = 9 枚が目安。全部 [0, 10] 範囲内。
    assert 6 <= len(times) <= 9
    assert all(0.0 < t < 10.0 for t in times)


def test_select_frame_times_from_cuts_backward_compat_default_2():
    """既定 frames_per_shot=2 は旧挙動を維持する。"""
    times_old = reference_v2.select_frame_times_from_cuts([2.0, 6.0], 10.0, max_frames=40)
    times_new = reference_v2.select_frame_times_from_cuts([2.0, 6.0], 10.0, max_frames=40, frames_per_shot=2)
    assert times_old == times_new


def test_resolve_frames_per_shot_supreme_plus_bumps_to_3():
    from pipeline import reference_v2 as rv2
    # 明示的な frames_per_shot 指定が最優先
    assert rv2._resolve_frames_per_shot({"reference": {"frames_per_shot": 3}}) == 3
    assert rv2._resolve_frames_per_shot({"reference": {"frames_per_shot": 1}}) == 1
    # supreme_plus は 3 に自動昇格
    assert rv2._resolve_frames_per_shot({"director_quality": "supreme_plus"}) == 3
    # それ以外は 2
    assert rv2._resolve_frames_per_shot({"director_quality": "supreme"}) == 2
    assert rv2._resolve_frames_per_shot({}) == 2


def test_enrich_shots_ref_fills_missing_p2_fields_from_vision_results():
    """LLM 融合結果に P2 拡張が欠けていても vision_results から shots_ref に補完される。"""
    from pipeline import reference_v2 as rv2
    spec = {
        "shots_ref": [
            {"index": 0, "start": 0.0, "end": 3.0,
             "visual_desc_en": "Serum bottle held up.", "motion": "static"},
            {"index": 1, "start": 3.0, "end": 6.0,
             "visual_desc_en": "Face closeup.", "motion": "person_talking"},
        ]
    }
    vision_results = [
        # shot0 の中の 3 フレーム: framing=product_on_hand が最頻、palette は #ffaa00 が高頻度
        {"time": 0.5, "framing": "product_on_hand", "location": "bathroom",
         "lighting": "soft daylight", "color_palette_hex": ["#ffaa00", "#111111"],
         "visual_desc_en": "Hands hold a bottle. Bright tiles behind. Static."},
        {"time": 1.5, "framing": "product_on_hand", "location": "bathroom",
         "lighting": "soft daylight", "color_palette_hex": ["#ffaa00", "#222222"],
         "visual_desc_en": "Bottle held up mid-frame. Ceramic counter. Slight tilt."},
        {"time": 2.5, "framing": "center", "location": "bathroom",
         "lighting": "soft daylight", "color_palette_hex": ["#3b2a1a"],
         "visual_desc_en": "Product label visible. Beige backdrop. Static."},
        # shot1
        {"time": 4.0, "framing": "closeup_face", "location": "bedroom",
         "lighting": "warm indoor", "color_palette_hex": ["#e6ccb2"],
         "visual_desc_en": "Woman's face. Bed pillows behind. Talking to camera."},
    ]
    stats = rv2._enrich_shots_ref_from_vision(spec, vision_results, 6.0)
    assert stats["enriched"] >= 2
    s0 = spec["shots_ref"][0]
    s1 = spec["shots_ref"][1]
    assert s0["framing"] == "product_on_hand"
    assert s0["location"] == "bathroom"
    assert s0["lighting"] == "soft daylight"
    # palette は頻度降順で ffaa00 が先頭のはず
    assert s0["color_palette_hex"][0] == "#ffaa00"
    assert s1["framing"] == "closeup_face"
    assert s1["location"] == "bedroom"
    assert s1["color_palette_hex"] == ["#e6ccb2"]


def test_enrich_shots_ref_preserves_existing_values():
    """既に framing 等が入っていれば上書きしない（LLM の融合結果を尊重）。"""
    from pipeline import reference_v2 as rv2
    spec = {
        "shots_ref": [
            {"index": 0, "start": 0.0, "end": 3.0,
             "visual_desc_en": "Existing rich description with three sentences included here to exceed 80 characters threshold clearly.",
             "framing": "aerial", "location": "outdoor",
             "lighting": "hard sunlight", "color_palette_hex": ["#012345"]},
        ]
    }
    vision_results = [
        {"time": 1.0, "framing": "product_on_hand", "location": "bathroom",
         "lighting": "neon", "color_palette_hex": ["#ffffff"],
         "visual_desc_en": "Something else."},
    ]
    rv2._enrich_shots_ref_from_vision(spec, vision_results, 3.0)
    s = spec["shots_ref"][0]
    assert s["framing"] == "aerial"
    assert s["location"] == "outdoor"
    assert s["lighting"] == "hard sunlight"
    assert s["color_palette_hex"] == ["#012345"]


def test_enrich_shots_ref_falls_back_when_llm_palette_is_malformed():
    """codex-review 指摘(P2): LLM が返した palette が大文字/0x/無効値だと下流で拾えないため、
    _normalize_palette_hex で正規化後が空なら vision 側 palette を採用する。
    """
    from pipeline import reference_v2 as rv2
    spec = {
        "shots_ref": [
            # 全部無効値の palette（vision に有効値がある場合はそちらを採用）
            {"index": 0, "start": 0.0, "end": 3.0,
             "visual_desc_en": "A.", "motion": "static",
             "color_palette_hex": ["red", "not-a-hex", "#zzzz"]},
        ]
    }
    vision_results = [
        {"time": 0.5, "color_palette_hex": ["#123456", "#654321"],
         "visual_desc_en": "Rich."},
        {"time": 1.5, "color_palette_hex": ["#123456"], "visual_desc_en": "More."},
    ]
    rv2._enrich_shots_ref_from_vision(spec, vision_results, 3.0)
    # 無効な既存 palette は捨てられ、vision 側の palette が採用される。
    assert spec["shots_ref"][0]["color_palette_hex"] == ["#123456", "#654321"]


def test_enrich_shots_ref_normalizes_uppercase_palette_case():
    """既存 palette が大文字表記でも下流の一貫性のため小文字に正規化する。"""
    from pipeline import reference_v2 as rv2
    spec = {
        "shots_ref": [
            {"index": 0, "start": 0.0, "end": 3.0,
             "visual_desc_en": "A.", "motion": "static",
             "color_palette_hex": ["#ABCDEF", "0x123456"]},
        ]
    }
    rv2._enrich_shots_ref_from_vision(spec, [{"time": 1.0, "color_palette_hex": ["#000000"]}], 3.0)
    assert spec["shots_ref"][0]["color_palette_hex"] == ["#abcdef", "#123456"]


def test_map_reference_visual_reads_p2_fields():
    """director が新4フィールドを skeleton の reference_visual まで運ぶ。"""
    from pipeline import director
    shots_ref = [{
        "index": 0, "start": 0.0, "end": 3.0,
        "visual_desc_en": "Bathroom serum.", "motion": "static",
        "shot_size": "closeup", "subject_count": 1, "camera_move": "none",
        "color_mood": "warm",
        "framing": "product_on_hand", "location": "bathroom",
        "lighting": "soft daylight", "color_palette_hex": ["#f4e6d0", "#3b2a1a"],
    }]
    rv = director._map_reference_visual_to_shots(shots_ref, [0.0, 3.0], ["s1"])
    r = rv["s1"]
    assert r["framing"] == "product_on_hand"
    assert r["location"] == "bathroom"
    assert r["lighting"] == "soft daylight"
    assert r["color_palette_hex"] == ["#f4e6d0", "#3b2a1a"]
