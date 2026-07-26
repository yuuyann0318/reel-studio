# -*- coding: utf-8 -*-
"""素肌リアル化（実ペア第2弾#5・美化禁止）の単体テスト。

参考(@ami_biyou_ KINUI)は毛穴・産毛・ほくろが見える実素肌。生成はプラスチック肌に美化
されていた。対策: skin_texture(raw|smooth|unknown) を vision→shots_ref→reference_visual に
貫通させ、参考が raw のときだけ higgsfield プロンプトに非美化指示＋negative を注入する。
参考が smooth（綺麗肌）のときは注入しない。Before/After アークは検出できたときだけ付与する。
"""
from pipeline.visual import higgsfield_backend as hb
from pipeline import reference_v2, director


# --- 注入の条件発動 ---------------------------------------------------------

def test_skin_injection_fires_on_raw_skin_texture():
    shot = {"visual_prompt": "a close up of a face",
            "reference_visual": {"has_person": True, "skin_texture": "raw"}}
    out = hb._append_skin_realism_directive("a close up of a face", shot, True)
    assert hb._SKIN_REALISM_MARKER in out.lower()
    assert "no plastic smooth skin" in out.lower()


def test_skin_injection_suppressed_on_smooth_reference():
    """参考が綺麗肌(smooth)なら注入しない（参考準拠が原則）。"""
    shot = {"visual_prompt": "a face",
            "reference_visual": {"has_person": True, "skin_texture": "smooth"}}
    out = hb._append_skin_realism_directive("a face", shot, True)
    assert hb._SKIN_REALISM_MARKER not in out.lower()
    assert out == "a face"


def test_skin_injection_from_person_keywords_when_texture_unknown():
    shot = {"visual_prompt": "x",
            "reference_visual": {"has_person": True, "desc_en": "close-up showing visible pores and redness"}}
    out = hb._append_skin_realism_directive("x", shot, True)
    assert hb._SKIN_REALISM_MARKER in out.lower()


def test_skin_injection_not_on_non_person_shot():
    shot = {"visual_prompt": "product bottle", "reference_visual": {"has_person": False, "desc_en": "a serum bottle"}}
    assert hb._append_skin_realism_directive("product bottle", shot, True) == "product bottle"


def test_skin_injection_disabled_flag():
    shot = {"visual_prompt": "x", "reference_visual": {"has_person": True, "skin_texture": "raw"}}
    assert hb._append_skin_realism_directive("x", shot, False) == "x"


def test_build_create_cmd_includes_skin_directive_for_raw():
    shot = {"id": "s1", "duration_sec": 4.0, "visual_prompt": "a face",
            "reference_visual": {"has_person": True, "skin_texture": "raw"}}
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", shot, "480p",
                               no_text_in_video=False, raw_skin_injection=True)
    prompt = cmd[cmd.index("--prompt") + 1]
    assert hb._SKIN_REALISM_MARKER in prompt.lower()


# --- skin_texture の正規化・貫通 --------------------------------------------

def test_normalize_skin_texture():
    assert reference_v2._normalize_skin_texture("raw") == "raw"
    assert reference_v2._normalize_skin_texture("SMOOTH") == "smooth"
    assert reference_v2._normalize_skin_texture("shiny") == "unknown"
    assert reference_v2._normalize_skin_texture(None) == "unknown"


def test_skin_texture_flows_into_reference_visual():
    """shots_ref.skin_texture が director の reference_visual まで貫通する。"""
    shots_ref = [{"start": 0.0, "end": 4.0, "visual_desc_en": "close up face",
                  "skin_texture": "raw", "has_person": True}]
    rv_map = director._map_reference_visual_to_shots(shots_ref, [0.0, 4.0], ["s1"])
    assert rv_map["s1"].get("skin_texture") == "raw"


def test_skin_texture_unknown_is_dropped_from_reference_visual():
    shots_ref = [{"start": 0.0, "end": 4.0, "visual_desc_en": "wide city", "skin_texture": "unknown"}]
    rv_map = director._map_reference_visual_to_shots(shots_ref, [0.0, 4.0], ["s1"])
    assert "skin_texture" not in (rv_map.get("s1") or {})


# --- transformation_arc: 無い場合は付与しない -------------------------------

def test_no_arc_when_all_raw():
    """全編 raw（この参考）のときはアークを付与しない。"""
    shots_ref = [
        {"start": 0.0, "end": 4.0, "skin_texture": "raw"},
        {"start": 4.0, "end": 8.0, "skin_texture": "raw"},
    ]
    assert reference_v2._detect_transformation_arc(shots_ref) is None


def test_arc_detected_on_raw_to_smooth():
    shots_ref = [
        {"start": 0.0, "end": 5.0, "skin_texture": "raw"},
        {"start": 5.0, "end": 10.0, "skin_texture": "smooth"},
    ]
    arc = reference_v2._detect_transformation_arc(shots_ref)
    assert arc == {"before_end_sec": 5.0, "after_start_sec": 5.0}


def test_skeleton_no_transformation_phase_without_arc():
    """アーク無しの spec では skeleton の shot に transformation_phase を付けない。"""
    spec = {
        "duration_sec": 8.0,
        "cuts": [{"t": 0.0}, {"t": 4.0}, {"t": 8.0}],
        "shots_ref": [
            {"start": 0.0, "end": 4.0, "visual_desc_en": "a", "skin_texture": "raw"},
            {"start": 4.0, "end": 8.0, "visual_desc_en": "b", "skin_texture": "raw"},
        ],
        "telops": [],
    }
    sk = director.build_shot_skeleton(spec, 8.0)
    for sh in sk["shots"]:
        assert "transformation_phase" not in sh


def test_skeleton_maps_transformation_phase_with_arc():
    spec = {
        "duration_sec": 10.0,
        "cuts": [{"t": 0.0}, {"t": 5.0}, {"t": 10.0}],
        "shots_ref": [
            {"start": 0.0, "end": 5.0, "visual_desc_en": "a", "skin_texture": "raw"},
            {"start": 5.0, "end": 10.0, "visual_desc_en": "b", "skin_texture": "smooth"},
        ],
        "telops": [],
        "transformation_arc": {"before_end_sec": 5.0, "after_start_sec": 5.0},
    }
    sk = director.build_shot_skeleton(spec, 10.0)
    phases = [sh.get("transformation_phase") for sh in sk["shots"]]
    assert "before" in phases and "after" in phases
