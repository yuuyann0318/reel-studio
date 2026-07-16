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
