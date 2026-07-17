# -*- coding: utf-8 -*-
from pipeline import director


def test_build_director_prompt_substitutes_placeholders():
    prompt = director.build_director_prompt("AI副業の始め方", 30, target_tolerance_sec=8)
    assert "AI副業の始め方" in prompt
    assert "30" in prompt
    assert "{THEME}" not in prompt
    assert "{TARGET_DURATION}" not in prompt
    assert "{SCHEMA_EXAMPLE}" not in prompt


def test_build_corrective_prompt_includes_errors():
    prompt = director.build_corrective_prompt("base prompt", ["エラー1", "エラー2"])
    assert "エラー1" in prompt
    assert "エラー2" in prompt
    assert "base prompt" in prompt


def test_run_director_no_llm_returns_rule_based_plan():
    plan = director.run_director("AIで副業を始める", config={}, target_duration_sec=20, no_llm=True)
    assert plan["meta"]["source"] == "rule"
    assert len(plan["shots"]) >= 3


def test_run_director_falls_back_when_claude_runner_unavailable(monkeypatch):
    """call_claude_json が None（claude_runner未使用環境）でもルールベース代替へフォールバックする。"""
    monkeypatch.setattr(director, "call_claude_json", None)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=False)
    assert plan["meta"]["source"] == "rule"


# --- vertical_hookスタイル ---------------------------------------------------

def test_build_director_prompt_uses_vertical_hook_template_when_style_specified():
    default_prompt = director.build_director_prompt("テーマ", 15, style="default")
    vh_prompt = director.build_director_prompt("テーマ", 15, style="vertical_hook")
    assert vh_prompt != default_prompt
    assert "vertical_hook" in vh_prompt
    assert "5部構成" in vh_prompt


def test_build_director_prompt_falls_back_to_default_for_unknown_style():
    default_prompt = director.build_director_prompt("テーマ", 15, style="default")
    unknown_prompt = director.build_director_prompt("テーマ", 15, style="not_a_style")
    assert unknown_prompt == default_prompt


def test_run_director_no_llm_vertical_hook_sets_meta_style_and_paces_shots():
    plan = director.run_director(
        "今話題のAI動画アプリを試した結果", config={}, target_duration_sec=15, no_llm=True, style="vertical_hook"
    )
    assert plan["meta"]["source"] == "rule"
    assert plan["meta"]["style"] == "vertical_hook"
    # 尺÷2秒目安（15秒->7〜8ショット）・各ショット1.5〜2.5秒程度の高速カット。
    assert 6 <= len(plan["shots"]) <= 9
    for shot in plan["shots"]:
        assert 1.5 <= shot["duration_sec"] <= 2.5


def test_run_director_no_llm_default_style_sets_meta_style_default():
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=True)
    assert plan["meta"]["style"] == "default"


def test_run_director_sets_meta_style_even_when_claude_runner_unavailable(monkeypatch):
    monkeypatch.setattr(director, "call_claude_json", None)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=False, style="vertical_hook")
    assert plan["meta"]["style"] == "vertical_hook"
