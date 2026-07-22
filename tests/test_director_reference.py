# -*- coding: utf-8 -*-
"""director.py の参考動画TTP注入(スケルトンJSON) / 骨検査 / 丸写し矯正リトライを検証する。

TTP v2 Phase 2 (2026-07-21):
- 従来の「beats/rhythm/telops のヒント注入」方式は撤去した。
- 参考動画から build_shot_skeleton() で組み立てたスケルトンをプロンプトへ注入し、
  LLM の役割は各 shot の narration_jp / caption_jp / visual_prompt を埋めることのみ。
- LLM 出力は骨(shot数・duration_sec・caption offset・sfx_plan・hook/cta shot_id)を
  保持しなければならない。

call_claude_json は一切実LLM呼び出しをせず、プロンプト本文に含まれるマーカー文字列で
write/critique を判別してモック応答を返す。
"""
import json
import os

import pytest

from pipeline import director


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _kind(prompt):
    if "前回の出力は以下のエラー" in prompt:
        return "corrective"
    if "部下の台本" in prompt:
        return "critique"
    return "write"


def _plan_from_skeleton(skeleton, narration_text="オリジナルなナレーションを入れる。"):
    """スケルトンをそのまま尊重した plan を組み立てる（LLM 完璧応答のシミュレーション）。"""
    shots = []
    for s in skeleton["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "abstract placeholder for shot {}".format(s["id"])
        shot["motion_preset"] = "static"
        shot["caption_jp"] = "テスト{}".format(s["id"])
        shot["narration_jp"] = narration_text
        shots.append(shot)
    return {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "テスト企画",
        "hook": "テストフック",
        "narration_script": narration_text * len(shots),
        "shots": shots,
        "sfx_plan": skeleton["sfx_plan"],
        "hook_end_shot_id": skeleton["hook_end_shot_id"],
        "cta_start_shot_id": skeleton["cta_start_shot_id"],
        "bgm_mood": "upbeat",
    }


# ---------------------------------------------------------------------------
# _build_reference_block: スケルトンJSONの注入
# ---------------------------------------------------------------------------

def test_build_reference_block_returns_empty_for_none_or_empty_skeleton():
    assert director._build_reference_block(None, 30) == ""
    assert director._build_reference_block({}, 30) == ""


def test_build_reference_block_injects_skeleton_json_and_ttp_header(real_spec):
    skeleton = director.build_shot_skeleton(real_spec, 30)
    block = director._build_reference_block(skeleton, 30)
    assert "参考動画の構成スケルトン" in block or "TTP" in block
    # スケルトンJSON がテキストに含まれていること
    assert '"shots"' in block
    assert '"sfx_plan"' in block
    assert '"hook_end_shot_id"' in block


def test_build_reference_block_contains_15_char_verbatim_warning(real_spec):
    skeleton = director.build_shot_skeleton(real_spec, 30)
    block = director._build_reference_block(skeleton, 30)
    assert "15字" in block


def test_build_director_prompt_injects_reference_block(real_spec):
    skeleton = director.build_shot_skeleton(real_spec, 20)
    block = director._build_reference_block(skeleton, 20)
    prompt = director.build_director_prompt("テーマ", 20, reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "参考動画の構成スケルトン" in prompt


def test_build_director_prompt_vertical_hook_injects_reference_block(real_spec):
    skeleton = director.build_shot_skeleton(real_spec, 20)
    block = director._build_reference_block(skeleton, 20)
    prompt = director.build_director_prompt("テーマ", 20, style="vertical_hook", reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "参考動画の構成スケルトン" in prompt


def test_build_critique_prompt_injects_reference_block(real_spec):
    skeleton = director.build_shot_skeleton(real_spec, 20)
    block = director._build_reference_block(skeleton, 20)
    draft = _plan_from_skeleton(skeleton)
    prompt = director.build_critique_prompt("テーマ", 20, 8, "default", draft, reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "参考動画の構成スケルトン" in prompt


# ---------------------------------------------------------------------------
# run_director: reference=None の明示エラー
# ---------------------------------------------------------------------------

def test_run_director_raises_ttp_reference_required_when_reference_missing():
    with pytest.raises(director.TTPReferenceRequiredError, match="--reference-url"):
        director.run_director("テストテーマ", config={}, target_duration_sec=20)


def test_run_director_no_llm_still_works_without_reference():
    """no_llm=True なら reference 無しでもスモーク plan を返す(テスト/CLI用途)。"""
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=True)
    assert plan["meta"]["source"] == "rule"  # build_rule_based_plan alias 経由


# ---------------------------------------------------------------------------
# run_director + reference: angles スキップ / スケルトン骨検査 / 丸写し検査
# ---------------------------------------------------------------------------

def test_run_director_ttp_mode_uses_skeleton_and_skips_angles(monkeypatch, real_spec):
    kinds_seen = []
    skeleton_from_prompt = None

    def fake_call_claude_json(prompt, timeout_sec=600):
        nonlocal skeleton_from_prompt
        kind = _kind(prompt)
        kinds_seen.append(kind)
        # プロンプトから注入されたスケルトンを取り出す代わりに、直接 build して同じ骨を返す。
        skeleton_from_prompt = director.build_shot_skeleton(real_spec, 30.0)
        return {"ok": True, "data": _plan_from_skeleton(skeleton_from_prompt), "model_used": "m", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=30.0, reference=real_spec)

    assert plan["meta"]["stages"]["angles"] == {"ok": False, "skipped": "reference"}
    assert "angles" not in kinds_seen
    assert "write" in kinds_seen
    assert "critique" in kinds_seen
    # スケルトンの骨が最終plan に保持されていること
    assert len(plan["shots"]) == len(skeleton_from_prompt["shots"])
    assert plan.get("sfx_plan") == skeleton_from_prompt["sfx_plan"]
    assert plan.get("hook_end_shot_id") == skeleton_from_prompt["hook_end_shot_id"]
    assert plan.get("cta_start_shot_id") == skeleton_from_prompt["cta_start_shot_id"]


def test_run_director_ttp_write_all_fail_raises(monkeypatch, real_spec):
    """LLM がスケルトンの骨を守れず矯正リトライも全滅したら例外(スモーク代替しない)。"""
    def fake_call_claude_json(prompt, timeout_sec=600):
        # 常に shots 空の invalid plan を返す
        return {
            "ok": True,
            "data": {
                "version": 2, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
                "narration_script": "n", "shots": [], "bgm_mood": "upbeat",
            },
            "model_used": "m", "error": None,
        }

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    with pytest.raises(director.TTPSkeletonMismatchError):
        director.run_director("テストテーマ", config={}, target_duration_sec=30.0, reference=real_spec)


def test_run_director_verbatim_overlap_triggers_retry(monkeypatch, real_spec):
    """参考動画のテロップ文言と15字以上連続一致すると矯正リトライされる。"""
    call_seq = {"count": 0}

    def fake_call_claude_json(prompt, timeout_sec=600):
        call_seq["count"] += 1
        skeleton = director.build_shot_skeleton(real_spec, 30.0)
        if call_seq["count"] == 1:
            # 参考テロップ(15字以上)をそのまま含む plan を返す
            bad_text = "Gorillo built a pond for a family of frogs"
            return {"ok": True, "data": _plan_from_skeleton(skeleton, bad_text),
                    "model_used": "m", "error": None}
        # 2回目以降: オリジナル日本語ナレーションに差し替える
        return {"ok": True, "data": _plan_from_skeleton(skeleton, "オリジナルの独自ナレーション文言に差し替えた"),
                "model_used": "m", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director(
        "テストテーマ", config={}, target_duration_sec=30.0, reference=real_spec, quality="single"
    )
    # 矯正リトライが少なくとも1回発生している
    assert call_seq["count"] >= 2
    # 最終 plan には丸写し文言が残っていない
    for s in plan["shots"]:
        assert "Gorillo built a pond" not in s.get("narration_jp", "")


def test_run_director_no_llm_ignores_reference_and_returns_rule_based_plan(real_spec):
    """no_llm=True は reference の有無を問わず build_rule_based_plan で返る（従来通り）。"""
    plan = director.run_director(
        "AIで副業を始める", config={}, target_duration_sec=20, no_llm=True, reference=real_spec
    )
    assert plan["meta"]["source"] == "rule"


# ---------------------------------------------------------------------------
# reference=None + no_llm=False: KR4 の明示エラー担保
# ---------------------------------------------------------------------------

def test_kr4_reference_none_llm_mode_raises_ttp_required():
    with pytest.raises(director.TTPReferenceRequiredError):
        director.run_director("テーマ", config={}, target_duration_sec=15, no_llm=False, reference=None)
