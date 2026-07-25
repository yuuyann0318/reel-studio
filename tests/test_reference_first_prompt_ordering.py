# -*- coding: utf-8 -*-
"""P2 KR2: higgsfield_backend._augment_prompt_with_reference_visual が
「参考shotの再現」を主文に据え、商品/テーマは挿入句として従属する構造になっていることを機械検証する。

背景:
  P1 でプロンプト序列は一本化したが、visual_prompt の主語は依然として商品/テーマ側だった
  （_augment_prompt_with_reference_visual が末尾補足として camera/shot_size/color_mood を
   足すだけだったため）。P2 では main clause = 参考shotの再現、subject clause = 商品/テーマ
  に置換して「参考動画が絶対ベース・商品が従属素材」を機械的に強制する。
"""
from __future__ import annotations

from pipeline.visual import higgsfield_backend


def _prompt(rv, base="a beige cosmetic serum bottle"):
    shot = {"id": "s1", "visual_prompt": base, "duration_sec": 5, "reference_visual": rv}
    cmd = higgsfield_backend._build_create_cmd("higgsfield", "seedance_2_0_mini", shot, "480p")
    return cmd[cmd.index("--prompt") + 1]


def test_main_clause_is_reference_recreation_when_desc_provided():
    """desc_en があれば prompt の先頭は "Recreate reference shot:" になり、base_prompt は
    "Subject in this shot:" として最後に挿入される（＝主文=参考再現、挿入句=商品）。
    """
    rv = {
        "desc_en": "Extreme close-up of a woman's hands squeezing serum onto her palm",
        "location": "bathroom",
        "framing": "product_on_hand",
        "lighting": "soft daylight",
        "camera_move": "none",
        "color_palette_hex": ["#f4e6d0", "#3b2a1a"],
        "color_mood": "warm",
    }
    p = _prompt(rv)
    assert p.startswith("Recreate reference shot:"), \
        "参考shot再現が主文ではない: {!r}".format(p[:200])
    assert "Subject in this shot: a beige cosmetic serum bottle" in p, \
        "商品/テーマが挿入句として最後尾に置かれていない: {!r}".format(p[-200:])
    # 参考記述が商品よりも前に来ていること（機械検証: 位置比較）
    idx_ref = p.find("Recreate reference shot:")
    idx_subj = p.find("Subject in this shot:")
    assert idx_ref < idx_subj, "参考shot記述が商品より後ろに来ている: {!r}".format(p)
    # location/framing/lighting/palette の英文が含まれる
    assert "in bathroom" in p
    assert "product-on-hand framing" in p
    assert "soft daylight lighting" in p
    assert "palette #f4e6d0" in p
    assert "warm color mood" in p


def test_backward_compat_no_reference_visual_returns_base_prompt():
    shot = {"id": "s1", "visual_prompt": "abstract geometric", "duration_sec": 5}
    # no_text_in_video を無効化すれば、reference_visual が無いとき base prompt はそのまま。
    cmd = higgsfield_backend._build_create_cmd(
        "higgsfield", "seedance_2_0_mini", shot, "480p", no_text_in_video=False
    )
    p = cmd[cmd.index("--prompt") + 1]
    assert p == "abstract geometric"


def test_no_desc_but_camera_and_color_mood_falls_back_gracefully():
    """desc_en が空でも従来の R2b テストが期待する camera/shot_size/color_mood 句は残す
    （既存テスト test_pan_left_added_to_prompt が壊れないこと）。"""
    rv = {"camera_move": "pan_l", "shot_size": "medium", "color_mood": "warm"}
    p = _prompt(rv, base="a girl in a park")
    assert "camera pans to the left" in p
    assert "medium shot composition" in p
    assert "warm color mood" in p
    assert "Subject in this shot: a girl in a park" in p


def test_camera_phrase_is_suppressed_when_base_prompt_has_camera_language():
    """base_prompt に既にカメラワーク文言があれば cam_phrase を追加しない（重複防止）。"""
    rv = {"camera_move": "pan_l", "color_mood": "warm"}
    base = "a shot where the camera pans slowly across a room"
    p = _prompt(rv, base=base)
    # camera pans は元base_prompt由来の1回のみ（追加句は入れない）
    assert p.lower().count("camera pans") == 1
    assert "warm color mood" in p


def test_palette_hex_only_flows_through():
    """palette があれば prompt に "palette #..." が含まれる（mock 側の gradients に効かせる下敷き）。"""
    rv = {
        "desc_en": "Aerial view of a Minecraft frog pond",
        "camera_move": "none",
        "color_palette_hex": ["#4CAF50", "#0d47a1", "#e6ccb2"],
    }
    p = _prompt(rv, base="a shiny green frog on a lily pad")
    assert "palette #4caf50 #0d47a1 #e6ccb2" in p


def test_intensity_strong_variant_preserved():
    """intensity=strong は camera 句に (strong) を残す（回帰確認）。"""
    rv = {"desc_en": "Push into the scene", "camera_move": "zoom_in", "intensity": "strong"}
    p = _prompt(rv, base="abstract shapes")
    assert "camera zooms in (strong)" in p
    assert "Recreate reference shot: Push into the scene" in p
