# -*- coding: utf-8 -*-
"""director.py の3段多段生成(quality="supreme")を検証する。

call_claude_json は一切実LLM呼び出しをせず、プロンプト本文に含まれる各段固有の
マーカー文字列で「angles / write(write用矯正リトライ含む) / critique(polish)」を
判別してモック応答を返す。
"""
from pipeline import director


def _valid_plan_dict(narration_marker, per_shot=5.0, shot_count=4):
    shots = []
    for i in range(shot_count):
        shots.append(
            {
                "id": "s{}".format(i + 1),
                "visual_prompt": "abstract geometric background {}".format(i + 1),
                "motion_preset": "zoom_in",
                "duration_sec": per_shot,
                "caption_jp": "テロップ{}".format(i + 1),
            }
        )
    return {
        "version": 1,
        "meta": {"source": "ai"},
        "concept": "テスト企画",
        "hook": "冒頭フック",
        "narration_script": narration_marker,
        "shots": shots,
        "bgm_mood": "upbeat",
    }


def _invalid_plan_dict():
    # hook が空文字・shots が空 -> validate_plan で必ず不合格になる。
    return {
        "version": 1,
        "meta": {"source": "ai"},
        "concept": "テスト企画",
        "hook": "",
        "narration_script": "",
        "shots": [],
        "bgm_mood": "upbeat",
    }


def _kind(prompt):
    if "chosen_index" in prompt:
        return "angles"
    if "部下の台本" in prompt:
        return "critique"
    return "write"


# ---------------------------------------------------------------------------
# (a) supreme 3段すべて成功時のmeta記録
# ---------------------------------------------------------------------------

def test_supreme_all_stages_succeed_records_meta(monkeypatch):
    captured = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        captured.append(_kind(prompt))
        kind = _kind(prompt)
        if kind == "angles":
            return {
                "ok": True,
                "data": {
                    "candidates": [
                        {"angle": "A", "viewpoint": "v1", "hook": "h1", "emotional_arc": "e1", "why_it_works": "w1"},
                        {"angle": "B", "viewpoint": "v2", "hook": "h2", "emotional_arc": "e2", "why_it_works": "w2"},
                        {"angle": "C", "viewpoint": "v3", "hook": "h3", "emotional_arc": "e3", "why_it_works": "w3"},
                    ],
                    "chosen_index": 1,
                    "chosen_reason": "理由テキスト",
                },
                "model_used": "claude-opus-4-8",
                "error": None,
            }
        if kind == "critique":
            return {
                "ok": True,
                "data": _valid_plan_dict("polished narration"),
                "model_used": "claude-fable-5",
                "error": None,
            }
        return {
            "ok": True,
            "data": _valid_plan_dict("draft narration"),
            "model_used": "claude-opus-4-8",
            "error": None,
        }

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan = director.run_director("AI副業の始め方", config={}, target_duration_sec=20, no_llm=False)

    assert plan["meta"]["quality"] == "supreme"
    assert plan["meta"]["source"] == "ai"
    assert plan["meta"]["stages"]["angles"]["ok"] is True
    assert plan["meta"]["stages"]["write"]["ok"] is True
    assert plan["meta"]["stages"]["polish"]["ok"] is True
    # 最終稿は polish(critique)段の出力。model_usedもpolish段の実測モデルになる。
    assert plan["narration_script"] == "polished narration"
    assert plan["meta"]["model_used"] == "claude-fable-5"
    assert captured == ["angles", "write", "critique"]


# ---------------------------------------------------------------------------
# (b) angles失敗→angle_block空で続行（write/polishは正常に進む）
# ---------------------------------------------------------------------------

def test_angles_failure_continues_with_empty_angle_block(monkeypatch):
    write_prompts = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        if kind == "angles":
            return {"ok": False, "data": None, "model_used": None, "error": "angles段の応答取得に失敗"}
        if kind == "critique":
            return {"ok": True, "data": _valid_plan_dict("polished narration"), "model_used": "claude-opus-4-8", "error": None}
        write_prompts.append(prompt)
        return {"ok": True, "data": _valid_plan_dict("draft narration"), "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, no_llm=False)

    assert plan["meta"]["stages"]["angles"]["ok"] is False
    assert plan["meta"]["stages"]["write"]["ok"] is True
    assert plan["meta"]["stages"]["polish"]["ok"] is True
    # angle_blockが空のまま続行しているので、write段のプロンプトに切り口見出しが混入しない。
    assert len(write_prompts) >= 1
    assert "# 採用する切り口" not in write_prompts[0]
    assert "{ANGLE_BLOCK}" not in write_prompts[0]


# ---------------------------------------------------------------------------
# (c) polish不合格→Stage2のドラフトを採用
# ---------------------------------------------------------------------------

def test_polish_failure_keeps_write_draft(monkeypatch):
    critique_calls = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        if kind == "angles":
            return {
                "ok": True,
                "data": {
                    "candidates": [{"angle": "A", "viewpoint": "v", "hook": "h", "emotional_arc": "e", "why_it_works": "w"}],
                    "chosen_index": 0,
                    "chosen_reason": "理由",
                },
                "model_used": "claude-opus-4-8",
                "error": None,
            }
        if kind == "critique":
            critique_calls.append(prompt)
            return {"ok": True, "data": _invalid_plan_dict(), "model_used": "claude-fable-5", "error": None}
        return {"ok": True, "data": _valid_plan_dict("draft narration"), "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, no_llm=False)

    assert plan["meta"]["stages"]["write"]["ok"] is True
    assert plan["meta"]["stages"]["polish"]["ok"] is False
    # ドラフト(write段の結果)がそのまま最終稿として採用されていること。
    assert plan["narration_script"] == "draft narration"
    assert plan["meta"]["source"] == "ai"
    assert plan["meta"]["model_used"] == "claude-opus-4-8"
    # POLISH_MAX_RETRIES=1なので critique は初回+矯正リトライ1回の計2回呼ばれる。
    assert len(critique_calls) == 2


# ---------------------------------------------------------------------------
# (d) single / no_llm の後方互換
# ---------------------------------------------------------------------------

def test_no_llm_backward_compatible_returns_rule_based_plan():
    plan = director.run_director("AIで副業を始める", config={}, target_duration_sec=20, no_llm=True)
    assert plan["meta"]["source"] == "rule"
    assert len(plan["shots"]) >= 3
    assert "stages" not in plan["meta"]


def test_quality_single_only_calls_write_stage(monkeypatch):
    kinds_seen = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kinds_seen.append(_kind(prompt))
        return {"ok": True, "data": _valid_plan_dict("single narration", per_shot=5.0, shot_count=3), "model_used": "claude-opus-4-8", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan = director.run_director("テストテーマ", config={}, target_duration_sec=15, no_llm=False, quality="single")

    assert plan["meta"]["quality"] == "single"
    assert plan["meta"]["source"] == "ai"
    assert "stages" not in plan["meta"]
    assert kinds_seen == ["write"]


# ---------------------------------------------------------------------------
# (e) {ANGLE_BLOCK} 置換（残留プレースホルダが無いこと）
# ---------------------------------------------------------------------------

def test_build_director_prompt_replaces_angle_block_placeholder_when_empty():
    prompt = director.build_director_prompt("テーマ", 20, angle_block="")
    assert "{ANGLE_BLOCK}" not in prompt


def test_build_director_prompt_replaces_angle_block_placeholder_when_present():
    prompt = director.build_director_prompt("テーマ", 20, angle_block="# 採用する切り口(この戦略で書くこと)\n切り口: テスト\n")
    assert "{ANGLE_BLOCK}" not in prompt
    assert "# 採用する切り口" in prompt
    assert "切り口: テスト" in prompt


def test_build_director_prompt_default_angle_block_is_empty_and_backward_compatible():
    # angle_block未指定でも従来どおり呼び出せる（後方互換）。
    prompt = director.build_director_prompt("テーマ", 20)
    assert "{ANGLE_BLOCK}" not in prompt
    assert "{THEME}" not in prompt


def test_vertical_hook_style_also_replaces_angle_block_placeholder():
    prompt = director.build_director_prompt("テーマ", 15, style="vertical_hook", angle_block="切り口ブロック")
    assert "{ANGLE_BLOCK}" not in prompt
    assert "切り口ブロック" in prompt


# ---------------------------------------------------------------------------
# angles段の緩い検証（run_angles_stage単体）
# ---------------------------------------------------------------------------

def test_run_angles_stage_returns_empty_block_when_call_claude_json_unavailable(monkeypatch):
    monkeypatch.setattr(director, "call_claude_json", None)
    angle_block, stage_meta = director.run_angles_stage("テーマ", {}, 20)
    assert angle_block == ""
    assert stage_meta["ok"] is False


def test_run_angles_stage_returns_empty_block_when_chosen_index_out_of_range(monkeypatch):
    def fake_call_claude_json(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {"candidates": [{"angle": "A"}], "chosen_index": 5},
            "model_used": "claude-opus-4-8",
            "error": None,
        }

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    angle_block, stage_meta = director.run_angles_stage("テーマ", {}, 20)
    assert angle_block == ""
    assert stage_meta["ok"] is False
    assert stage_meta["model_used"] == "claude-opus-4-8"


def test_run_angles_stage_builds_block_on_success(monkeypatch):
    def fake_call_claude_json(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {
                "candidates": [{"angle": "失敗体験からの逆転型", "viewpoint": "当事者", "hook": "h", "emotional_arc": "共感→驚き", "why_it_works": "理由"}],
                "chosen_index": 0,
                "chosen_reason": "最も停止力が高いため",
            },
            "model_used": "claude-opus-4-8",
            "error": None,
        }

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    angle_block, stage_meta = director.run_angles_stage("テーマ", {}, 20)
    assert stage_meta["ok"] is True
    assert "失敗体験からの逆転型" in angle_block
    assert "最も停止力が高いため" in angle_block
