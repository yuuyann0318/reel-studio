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


def test_validate_plan_max_caption_chars_is_30_bug54():
    """BUG-54: caption 上限が 30字。31字はNG、30字はOK。"""
    assert plan_schema.MAX_CAPTION_CHARS == 30
    plan_ok = _valid_plan()
    plan_ok["shots"][0]["caption_jp"] = "あ" * 30
    ok, _errs, _ = plan_schema.validate_plan(plan_ok)
    assert ok, "30字ちょうどはOKになるべき"
    plan_ng = _valid_plan()
    plan_ng["shots"][0]["caption_jp"] = "あ" * 31
    ok_ng, errs_ng, _ = plan_schema.validate_plan(plan_ng)
    assert not ok_ng
    assert any("30字" in e or "caption_jp" in e for e in errs_ng)


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


def test_build_smoke_plan_has_valid_shape():
    """T2 相当: build_smoke_plan の形が validate_plan を通ることの薄い検査。

    「テーマ文字列を含む narration/caption を最低限持つ」検査に縮小した
    （TTP v2 移行で 3ポイント固定テンプレは撤去）。
    """
    plan = plan_schema.build_smoke_plan("AIで副業を始める", target_duration_sec=20, shot_count=4)
    ok, errors, normalized = plan_schema.validate_plan(plan, target_duration_sec=20, target_tolerance_sec=5)
    assert ok is True, errors
    assert normalized["meta"]["source"] == "smoke"
    assert normalized["meta"]["smoke"] is True
    assert len(normalized["shots"]) == 4
    # テーマ文字列が narration_script / caption_jp に反映されている
    assert "AIで副業を始める" in normalized["narration_script"]
    assert any("AIで副業を始める" in s["caption_jp"] for s in normalized["shots"])


# --- scene_id（シーングループ） ---------------------------------------------

def test_scene_id_omitted_backward_compatible():
    """scene_id 無しの従来plan: 正規化後にも scene_id キーは付かない（後方互換）。"""
    ok, errors, normalized = plan_schema.validate_plan(_valid_plan())
    assert ok is True
    assert all("scene_id" not in s for s in normalized["shots"])


def test_scene_id_passthrough_when_present():
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "p", "motion_preset": "zoom_in", "duration_sec": 2.0, "caption_jp": "a", "scene_id": "sc1"},
        {"id": "s2", "visual_prompt": "p", "motion_preset": "zoom_in", "duration_sec": 2.0, "caption_jp": "b", "scene_id": "sc1"},
        {"id": "s3", "visual_prompt": "p", "motion_preset": "pan_left", "duration_sec": 2.0, "caption_jp": "c", "scene_id": "sc2"},
    ])
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True, errors
    assert [s.get("scene_id") for s in normalized["shots"]] == ["sc1", "sc1", "sc2"]


def test_scene_id_consecutive_reuse_ok():
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "a", "scene_id": "sc1"},
        {"id": "s2", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "b", "scene_id": "sc1"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is True, errors


def test_scene_id_non_consecutive_reuse_rejected():
    """飛び飛び（sc1 → sc2 → sc1）の scene_id 再利用はエラー。"""
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "a", "scene_id": "sc1"},
        {"id": "s2", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "b", "scene_id": "sc2"},
        {"id": "s3", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "c", "scene_id": "sc1"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("scene_id" in e and "連続" in e for e in errors)


def test_scene_id_visual_prompt_mismatch_rejected():
    """同一 scene_id 内で visual_prompt が異なる plan はエラー（シーンマスター1本から
    切り出す前提のため）。矯正リトライへ回すためのハードエラー（[中]バグ修正）。
    """
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "prompt A", "motion_preset": "static", "duration_sec": 2.0,
         "caption_jp": "a", "scene_id": "sc1"},
        {"id": "s2", "visual_prompt": "prompt B", "motion_preset": "static", "duration_sec": 2.0,
         "caption_jp": "b", "scene_id": "sc1"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("visual_prompt" in e and "一致" in e for e in errors)


def test_scene_id_visual_prompt_match_ok():
    """同一 scene_id で visual_prompt が一致していれば validate_plan は通る。"""
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "same prompt", "motion_preset": "static", "duration_sec": 2.0,
         "caption_jp": "a", "scene_id": "sc1"},
        {"id": "s2", "visual_prompt": "same prompt", "motion_preset": "static", "duration_sec": 2.0,
         "caption_jp": "b", "scene_id": "sc1"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is True, errors


def test_scene_id_non_string_rejected():
    plan = _valid_plan(shots=[
        {"id": "s1", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "a", "scene_id": 123},
        {"id": "s2", "visual_prompt": "p", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "b"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("scene_id" in e for e in errors)


def test_build_smoke_plan_template_text_has_no_ng_words():
    """T3 相当: スモーク plan の固定文言に景表法NGワードが混入しないことを検証。

    テーマ文字列そのものにNGワードが含まれるケースの防止は compliance.py の役割
    （このテストはテンプレート文言側に限定して検証する）。
    """
    plan = plan_schema.build_smoke_plan("AIで副業を始める", target_duration_sec=20)
    full_text = plan["narration_script"] + plan["hook"] + "".join(s["caption_jp"] for s in plan["shots"])
    assert "絶対稼げる" not in full_text
    assert "100%成功" not in full_text


# T4/T5/T6/T8: vertical_hook 固定テンプレ(shot_count=尺÷2秒、5部構成TTP文言、
# 二重prefix回避)は撤去したためテストごと削除した。

def test_build_smoke_plan_unknown_style_normalizes_to_default():
    """T7 相当: 未知 style は 'default' に正規化される。"""
    plan = plan_schema.build_smoke_plan("テストテーマ", target_duration_sec=20, style="not_a_real_style")
    assert plan["meta"]["style"] == "default"


# --- narration_jp（音声主導タイミング同期モード用の任意フィールド） ---------------------


def test_validate_plan_narration_jp_optional_absent_is_backward_compatible():
    """narration_jpを一切含まないplan（従来のdirector出力）は今までどおり合法。"""
    plan = _valid_plan()
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True
    assert "narration_jp" not in normalized["shots"][0]
    assert "narration_jp" not in normalized["shots"][1]


def test_validate_plan_narration_jp_valid_string_is_normalized_and_stripped():
    plan = _valid_plan()
    plan["shots"][0]["narration_jp"] = "  これはナレーション断片です。  "
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True
    assert normalized["shots"][0]["narration_jp"] == "これはナレーション断片です。"
    # 指定しなかった方のshotには依然としてキーが付与されない
    assert "narration_jp" not in normalized["shots"][1]


def test_validate_plan_narration_jp_rejects_non_string():
    plan = _valid_plan()
    plan["shots"][0]["narration_jp"] = 12345
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is False
    assert normalized is None
    assert any("narration_jp" in e for e in errors)


# --- plan v2 拡張 (caption_in/out_offset_sec / telop_style_hint / sfx_plan / hook_end_shot_id / cta_start_shot_id) ---

def _v2_plan(shots=None, sfx_plan=None, hook_end_shot_id=None, cta_start_shot_id=None):
    plan = {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "v2テストコンセプト",
        "hook": "v2テストフック",
        "narration_script": "v2テストナレーション。",
        "shots": shots if shots is not None else [
            {"id": "s1", "visual_prompt": "bg1", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト1"},
            {"id": "s2", "visual_prompt": "bg2", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト2"},
        ],
        "bgm_mood": "upbeat",
    }
    if sfx_plan is not None:
        plan["sfx_plan"] = sfx_plan
    if hook_end_shot_id is not None:
        plan["hook_end_shot_id"] = hook_end_shot_id
    if cta_start_shot_id is not None:
        plan["cta_start_shot_id"] = cta_start_shot_id
    return plan


def test_validate_plan_v2_accepted():
    """version=2 は正常に受理される（v1 も引き続き受理: 後方互換）。"""
    ok, errors, normalized = plan_schema.validate_plan(_v2_plan())
    assert ok is True, errors
    assert normalized["version"] == 2


def test_validate_plan_v1_still_accepted():
    ok, errors, normalized = plan_schema.validate_plan(_valid_plan())
    assert ok is True, errors
    assert normalized["version"] == 1


def test_validate_plan_rejects_unknown_version():
    plan = _v2_plan()
    plan["version"] = 99
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("version" in e for e in errors)


def test_validate_plan_caption_offsets_within_shot_duration():
    plan = _v2_plan(shots=[
        {"id": "s1", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0,
         "caption_jp": "テスト", "caption_in_offset_sec": 1.0, "caption_out_offset_sec": 4.0},
        {"id": "s2", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト"},
    ])
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True, errors
    assert normalized["shots"][0]["caption_in_offset_sec"] == 1.0
    assert normalized["shots"][0]["caption_out_offset_sec"] == 4.0


def test_validate_plan_caption_in_offset_out_of_range_rejected():
    plan = _v2_plan(shots=[
        {"id": "s1", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0,
         "caption_jp": "テスト", "caption_in_offset_sec": 10.0},
        {"id": "s2", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("caption_in_offset_sec" in e for e in errors)


def test_validate_plan_caption_out_before_in_rejected():
    plan = _v2_plan(shots=[
        {"id": "s1", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0,
         "caption_jp": "テスト", "caption_in_offset_sec": 4.0, "caption_out_offset_sec": 2.0},
        {"id": "s2", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("caption_out_offset_sec" in e for e in errors)


def test_validate_plan_telop_style_hint_must_be_dict():
    plan = _v2_plan(shots=[
        {"id": "s1", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0,
         "caption_jp": "テスト", "telop_style_hint": "not-a-dict"},
        {"id": "s2", "visual_prompt": "bg", "motion_preset": "static", "duration_sec": 5.0, "caption_jp": "テスト"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("telop_style_hint" in e for e in errors)


def test_validate_plan_sfx_plan_valid():
    plan = _v2_plan(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s1", "offset_sec": 4.5}, "family": "whoosh"},
        {"t_anchor": {"type": "shot_start", "shot_id": "s2", "offset_sec": 0.0}, "family": "impact", "gain_db": -6},
    ])
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True, errors
    assert len(normalized["sfx_plan"]) == 2


def test_validate_plan_sfx_plan_rejects_unknown_family():
    plan = _v2_plan(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s1", "offset_sec": 0.0}, "family": "explosion"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("family" in e for e in errors)


def test_validate_plan_sfx_plan_rejects_unknown_anchor_type():
    plan = _v2_plan(sfx_plan=[
        {"t_anchor": {"type": "randomly", "shot_id": "s1", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("type" in e for e in errors)


def test_validate_plan_sfx_plan_rejects_shot_id_not_in_shots():
    plan = _v2_plan(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s99", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("shot_id" in e for e in errors)


def test_validate_plan_sfx_plan_rejects_offset_exceeds_shot_duration():
    plan = _v2_plan(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s1", "offset_sec": 99.0}, "family": "whoosh"},
    ])
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("offset_sec" in e for e in errors)


def test_validate_plan_hook_end_shot_id_must_exist():
    plan = _v2_plan(hook_end_shot_id="s99")
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("hook_end_shot_id" in e for e in errors)


def test_validate_plan_cta_start_shot_id_must_exist():
    plan = _v2_plan(cta_start_shot_id="s99")
    ok, errors, _ = plan_schema.validate_plan(plan)
    assert ok is False
    assert any("cta_start_shot_id" in e for e in errors)


def test_validate_plan_hook_cta_shot_id_ok_when_exists():
    plan = _v2_plan(hook_end_shot_id="s1", cta_start_shot_id="s2")
    ok, errors, normalized = plan_schema.validate_plan(plan)
    assert ok is True, errors
    assert normalized["hook_end_shot_id"] == "s1"
    assert normalized["cta_start_shot_id"] == "s2"
