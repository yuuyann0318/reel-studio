# -*- coding: utf-8 -*-
"""F10: reference_visual が skeleton の各 shot に注入され、director プロンプト系が
参考映像情報を最優先の根拠として書く指示に切り替わっていることの単体テスト。

背景:
  監査 F10:
  1. skeleton_json は shots に visual_desc_en / motion を持たず、director が
     shots_ref を丸ごと捨てていた。
  2. director_prompt.txt が「smartphone footage, bathroom / bedroom / drugstore shelf」
     を強制していたため、参考が料理・旅行・Minecraft でも生成 visual_prompt は
     浴室に化ける。

  ここではまず (1) skeleton 側で reference_visual が shot に載ることを機械検証し、
  (2) プロンプトファイル群の実文言が「参考映像を最優先」に切り替わったことを
  文字列マッチで確認する（LLM 生成そのものはテストせず、指示テンプレの内容だけを確認）。
"""
from __future__ import annotations

import json
import os

import pytest

from pipeline import director


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# (a) skeleton の各 shot に reference_visual が入る
# ---------------------------------------------------------------------------

def test_skeleton_shots_carry_reference_visual_from_shots_ref(real_spec):
    sk = director.build_shot_skeleton(real_spec, 30.0)
    shots = sk["shots"]
    # 実キャッシュspecは shots_ref が5個あり、cuts=4(=5区間) と対応するはず
    assert any("reference_visual" in s for s in shots), \
        "少なくとも1つの shot に reference_visual が載っていること"
    rv = next(s["reference_visual"] for s in shots if "reference_visual" in s)
    assert isinstance(rv, dict)
    # 参考の visual_desc_en が desc_en として入っていること（旧spec互換: motion/desc_en は必ずある）
    assert rv.get("desc_en"), "reference_visual.desc_en が空"
    assert rv.get("motion") in ("static", "pan_left", "pan_right", "zoom_in", "zoom_out",
                                  "person_talking", "cut_transition", "handheld", "dolly",
                                  "tilt_up", "tilt_down", "ken_burns", "none")


def test_skeleton_reference_visual_survives_credit_split(real_spec):
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    # クレジット意識分割時、同じ絵から切り出した断片には reference_visual をコピーする
    rv_shots = [s for s in sk["shots"] if "reference_visual" in s]
    assert rv_shots, "分割後の shot にも reference_visual が残っていること"


def test_build_shot_skeleton_supports_configurable_min_shot_sec(real_spec):
    """F12: supreme_plus プリセットで min_shot_sec を 0.5s に落として高速カットを保持する。"""
    sk_default = director.build_shot_skeleton(real_spec, 30.0)
    sk_relaxed = director.build_shot_skeleton(real_spec, 30.0, min_shot_sec=0.5)
    # 参考 cuts=4 なので shot は5個以下（既定 1.2s と 0.5s ではショット数が変わる可能性あり）
    assert len(sk_default["shots"]) >= 1
    assert len(sk_relaxed["shots"]) >= 1
    # duration_sec は全て min_shot_sec 以上
    for s in sk_relaxed["shots"]:
        assert s["duration_sec"] >= 0.5 - 1e-6


# ---------------------------------------------------------------------------
# (b) reference_ttp_block.txt に「reference_visual」の指示が入っている
# ---------------------------------------------------------------------------

def test_reference_ttp_block_instructs_visual_prompt_to_use_reference_visual():
    with open(os.path.join(os.path.dirname(director.__file__), "prompts",
                            "reference_ttp_block.txt"), "r", encoding="utf-8") as f:
        text = f.read()
    assert "reference_visual" in text
    # 「参考の映像情報を最優先の根拠にする」の趣旨がテンプレに残っていること
    assert "参考動画の実映像から抽出" in text or "第一の" in text


# ---------------------------------------------------------------------------
# (c) director_prompt.txt / vertical_hook から美容UGC強制テンプレが緩和されている
# ---------------------------------------------------------------------------

def _read_prompt(name):
    with open(os.path.join(os.path.dirname(director.__file__), "prompts", name),
              "r", encoding="utf-8") as f:
        return f.read()


def test_director_prompt_no_longer_forces_beauty_ugc_template():
    text = _read_prompt("director_prompt.txt")
    # 旧テンプレの断定的な語句「限定」は無くなっていること
    assert "『スマホで撮ったリアルな生活感』に限定" not in text
    # 新方針の指示が入っていること
    assert "参考動画の映像を最優先の根拠にする" in text
    assert "reference_visual" in text


def test_vertical_hook_prompt_no_longer_forces_beauty_ugc_template():
    text = _read_prompt("director_prompt_vertical_hook.txt")
    assert "『スマホで撮ったリアルな生活感』に限定" not in text
    assert "参考動画の映像を最優先の根拠にする" in text
    assert "reference_visual" in text


# ---------------------------------------------------------------------------
# (d) _build_reference_block が skeleton_json に reference_visual を含める
# ---------------------------------------------------------------------------

def test_reference_block_json_contains_reference_visual(real_spec):
    sk = director.build_shot_skeleton(real_spec, 30.0)
    block = director._build_reference_block(sk, 30.0)
    assert "reference_visual" in block, \
        "reference_block テンプレの skeleton_json に reference_visual フィールドが含まれていない"
