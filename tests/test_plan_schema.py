# -*- coding: utf-8 -*-
from pipeline import plan_schema


def _valid_plan(shots=None):
    return {
        "version": 1,
        "meta": {"source": "ai"},
        "concept": "テストコンセプト",
        "hook": "テストフック",
        "narration_script": "これはテストナレーションです。",
        "shots": shots if shots is not None else [
            {"id": "s1", "visual_prompt": "abstract background", "motion_preset": "zoom_in", "duration_sec": 5.0, "caption_jp": "テスト1"},
            {"id": "s2", "visual_prompt": "abstract icon", "motion_preset": "pan_left", "duration_sec": 5.0, "caption_jp": "テスト2"},
        ],
        "bgm_mood": "upbeat",
    }


def test_validate_plan_ok():
    ok, errors, normalized = plan_schema.validate_plan(_valid_plan())
    assert ok is True
    assert errors == []
    assert normalized["shots"][0]["id"] == "s1"
    assert normalized["bgm_mood"] == "upbeat"


def test_validate_plan_missing_fields():
    plan = _valid_plan()
    del plan["concept"]
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is False
    assert normalized is None
    assert any("concept" in e for e in errors)


def test_validate_plan_bad_motion_preset():
    plan = _valid_plan()
    plan["shots"][0]["motion_preset"] = "spin_around"
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("motion_preset" in e for e in errors)


def test_validate_plan_caption_too_long():
    plan = _valid_plan()
    plan["shots"][0]["caption_jp"] = "あ" * 100
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("caption_jp" in e for e in errors)


def test_validate_plan_duration_out_of_target_tolerance():
    plan = _valid_plan()
    ok, errors, _ = plan_schema.validate_plan(plan, target_duration_sec=60, target_tolerance_sec=5)
    assert ok is False
    assert any("目標から外れています" in e for e in errors)


def test_validate_plan_duration_within_target_tolerance():
    plan = _valid_plan()  # 合計10秒
    ok, errors, normalized = plan_schema.validate_plan(plan, target_duration_sec=10, target_tolerance_sec=3)
    assert ok is True
    assert normalized is not None


def test_validate_plan_duplicate_ids_rejected():
    plan = _valid_plan()
    plan["shots"][1]["id"] = "s1"
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("重複" in e for e in errors)


def test_build_rule_based_plan_has_valid_shape():
    plan = plan_schema.build_rule_based_plan("AIで副業を始める", target_duration_sec=20, shot_count=4)
    ok, errors, normalized = plan_schema.validate_plan(plan, target_duration_sec=20, target_tolerance_sec=5)
    assert ok is True, errors
    assert normalized["meta"]["source"] == "rule"
    assert len(normalized["shots"]) == 4


def test_build_rule_based_plan_template_text_has_no_ng_words():
    """テンプレート自体の固定文言（narration_scriptの定型部分）にNGワードが無いことを確認する。

    テーマ文字列そのものにNGワードが含まれるケースの防止は compliance.py の役割
    （このテストはテンプレート文言側に限定して検証する）。
    """
    plan = plan_schema.build_rule_based_plan("AIで副業を始める", target_duration_sec=20)
    full_text = plan["narration_script"] + "".join(s["caption_jp"] for s in plan["shots"])
    assert "絶対稼げる" not in full_text
    assert "100%成功" not in full_text
