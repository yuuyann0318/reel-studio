# -*- coding: utf-8 -*-
"""director.py の参考動画TTP注入({REFERENCE_TTP_BLOCK})・anglesスキップ・丸写し矯正リトライを検証する。

call_claude_json は一切実LLM呼び出しをせず、プロンプト本文に含まれるマーカー文字列で
angles/write(矯正リトライ含む)/critique(polish)を判別してモック応答を返す
（test_director_v2.pyのパターンを踏襲）。
"""
import hashlib

from pipeline import director


_SPEC = {
    "duration_sec": 20.0,
    "transcript": "今日はとても大事なポイントを三つに絞って徹底的に紹介していきます",
    "beats": [
        {"role": "hook", "start": 0.0, "end": 4.0, "text": "hook原文", "summary": "フックの要約"},
        {"role": "develop", "start": 4.0, "end": 10.0, "text": "develop原文", "summary": "展開の要約"},
        {"role": "proof", "start": 10.0, "end": 16.0, "text": "proof原文", "summary": "証拠の要約"},
        {"role": "cta", "start": 16.0, "end": 20.0, "text": "cta原文", "summary": "CTAの要約"},
    ],
    "rhythm": {
        "sentence_count": 6, "avg_sentence_len": 14.5, "max_sentence_len": 28,
        "tone": "テンポの速い断定調", "endings": ["〜です", "〜んです", "〜ました"],
    },
    "telops": None,
}


def _valid_plan_dict(narration, shot_count=2, per_shot=10.0):
    shots = [
        {
            "id": "s{}".format(i + 1), "visual_prompt": "abstract bg {}".format(i + 1),
            "motion_preset": "zoom_in", "duration_sec": per_shot, "caption_jp": "テロップ{}".format(i + 1),
        }
        for i in range(shot_count)
    ]
    return {
        "version": 1, "meta": {"source": "ai"}, "concept": "テスト企画", "hook": "冒頭フック",
        "narration_script": narration, "shots": shots, "bgm_mood": "upbeat",
    }


def _kind(prompt):
    if "chosen_index" in prompt:
        return "angles"
    if "前回の出力は以下のエラー" in prompt:
        return "corrective"
    if "部下の台本" in prompt:
        return "critique"
    return "write"


# ---------------------------------------------------------------------------
# _build_reference_block: ビートテーブル/リズム/テロップ整形
# ---------------------------------------------------------------------------

def test_build_reference_block_returns_empty_for_none_or_empty_spec():
    assert director._build_reference_block(None, 30) == ""
    assert director._build_reference_block({}, 30) == ""


def test_build_reference_block_contains_header_and_beats_scaled_to_target_duration():
    block = director._build_reference_block(_SPEC, 30)
    assert "# 参考動画のTTP" in block
    assert "15字以上" in block
    # 参考尺20秒 -> 目標30秒へスケール。hookは4秒/20秒=20% -> 30*0.2=6.0秒
    assert "[hook] 目安6.0秒(全体の20%)" in block
    assert "フックの要約" in block


def test_build_reference_block_includes_rhythm_summary():
    block = director._build_reference_block(_SPEC, 30)
    assert "テンポの速い断定調" in block
    assert "〜です" in block


def test_build_reference_block_omits_telops_section_when_null():
    block = director._build_reference_block(_SPEC, 30)
    assert "参考動画のテロップ例" not in block


def test_build_reference_block_includes_telops_section_when_present():
    spec = dict(_SPEC)
    spec["telops"] = ["ここがポイント", "見逃し注意"]
    block = director._build_reference_block(spec, 30)
    assert "参考動画のテロップ例" in block
    assert "ここがポイント" in block


def test_format_telops_list_returns_empty_for_non_list_types():
    # telopsがlist以外(不正な入力/壊れたキャッシュ)の場合、文字列を1文字ずつ箇条書きにする
    # ような壊れた出力を作らず、空文字を返す(=セクションごと省略される)べき。
    assert director._format_telops_list("文字列telops") == ""
    assert director._format_telops_list(123) == ""
    assert director._format_telops_list({"a": 1}) == ""
    assert director._format_telops_list([]) == ""
    assert director._format_telops_list(None) == ""


def test_build_reference_block_omits_telops_section_when_telops_is_a_string():
    spec = dict(_SPEC)
    spec["telops"] = "壊れたtelops文字列"
    block = director._build_reference_block(spec, 30)
    assert "参考動画のテロップ例" not in block
    # 文字列を1文字ずつ箇条書きにするような壊れた出力が混入していないことを確認
    assert "- 壊" not in block


# ---------------------------------------------------------------------------
# {REFERENCE_TTP_BLOCK} 注入（各プロンプトへのプレースホルダ解決）
# ---------------------------------------------------------------------------

def test_build_director_prompt_injects_reference_block():
    block = director._build_reference_block(_SPEC, 20)
    prompt = director.build_director_prompt("テーマ", 20, reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "# 参考動画のTTP" in prompt


def test_build_director_prompt_vertical_hook_injects_reference_block():
    block = director._build_reference_block(_SPEC, 20)
    prompt = director.build_director_prompt("テーマ", 20, style="vertical_hook", reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "# 参考動画のTTP" in prompt


def test_build_critique_prompt_injects_reference_block():
    block = director._build_reference_block(_SPEC, 20)
    draft = _valid_plan_dict("draft")
    prompt = director.build_critique_prompt("テーマ", 20, 8, "default", draft, reference_block=block)
    assert "{REFERENCE_TTP_BLOCK}" not in prompt
    assert "# 参考動画のTTP" in prompt


# ---------------------------------------------------------------------------
# 空置換のバイト等価回帰: reference未使用時は既存プロンプト出力が完全一致
# （このハッシュはreference_block/REFERENCE_TTP_BLOCKプレースホルダ導入前の
#  build_director_prompt/build_critique_prompt出力から採取した既知値）。
# ---------------------------------------------------------------------------

_EXPECTED_SHA256 = {
    "default_basic": "f1a17b0709b869c6380f5ce2be57c2510821bdafafb8b98014c6bcfa046534a3",
    "default_with_angle": "c8c87adbdb7f897841fefaf2ff506e4246aaab0e9528ad6942849c46171759ef",
    "vertical_hook_basic": "d12da123c0eec87ae1e07130726a0d92262d19491fd2f4897bc1cc107033e598",
    "default_with_product": "4de982684ce4c2c78c8826226cc12280d3507f665103232b4422a30c9080a742",
    "critique_basic": "5f380e4f172e4d47c5154168bceb6444fbee3a3da84be1fa14989ec0f415f81c",
}


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_build_director_prompt_byte_identical_to_pre_reference_baseline_when_no_reference():
    prompt = director.build_director_prompt("AI副業の始め方", 30)
    assert _sha256(prompt) == _EXPECTED_SHA256["default_basic"]


def test_build_director_prompt_with_angle_byte_identical_when_no_reference():
    prompt = director.build_director_prompt(
        "AI副業の始め方", 30, angle_block="# 採用する切り口(この戦略で書くこと)\n切り口: テスト\n"
    )
    assert _sha256(prompt) == _EXPECTED_SHA256["default_with_angle"]


def test_build_director_prompt_vertical_hook_byte_identical_when_no_reference():
    prompt = director.build_director_prompt("テーマ", 15, style="vertical_hook")
    assert _sha256(prompt) == _EXPECTED_SHA256["vertical_hook_basic"]


def test_build_director_prompt_with_product_byte_identical_when_no_reference():
    product = {"name": "商品A", "url": "https://x", "image_count": 3}
    prompt = director.build_director_prompt("テーマ", 15, product=product)
    assert _sha256(prompt) == _EXPECTED_SHA256["default_with_product"]


def test_build_critique_prompt_byte_identical_when_no_reference():
    draft = {
        "version": 1, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
        "narration_script": "n", "shots": [], "bgm_mood": "upbeat",
    }
    prompt = director.build_critique_prompt("テーマ", 20, 8, "default", draft)
    assert _sha256(prompt) == _EXPECTED_SHA256["critique_basic"]


def test_run_director_reference_none_default_matches_no_reference_call(monkeypatch):
    """reference引数を省略した場合と reference=None を明示した場合とで生成物が完全一致すること。"""
    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        if kind == "angles":
            return {
                "ok": True,
                "data": {
                    "candidates": [{"angle": "A", "viewpoint": "v", "hook": "h", "emotional_arc": "e", "why_it_works": "w"}],
                    "chosen_index": 0, "chosen_reason": "理由",
                },
                "model_used": "claude-opus-4-8",
            }
        return {"ok": True, "data": _valid_plan_dict("narration", shot_count=2, per_shot=10.0), "model_used": "claude-opus-4-8"}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan_default = director.run_director("テストテーマ", config={}, target_duration_sec=20)
    plan_explicit_none = director.run_director("テストテーマ", config={}, target_duration_sec=20, reference=None)
    assert plan_default["narration_script"] == plan_explicit_none["narration_script"]
    assert plan_default["meta"]["stages"] == plan_explicit_none["meta"]["stages"]


# ---------------------------------------------------------------------------
# angles段のスキップ（参考動画ありのとき）
# ---------------------------------------------------------------------------

def test_run_director_skips_angles_stage_when_reference_given(monkeypatch):
    captured_kinds = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        captured_kinds.append(kind)
        if kind == "angles":
            raise AssertionError("参考動画ありのときはangles段が呼ばれてはいけない")
        if kind == "critique":
            return {"ok": True, "data": _valid_plan_dict("polished", shot_count=2, per_shot=10.0), "model_used": "m"}
        return {"ok": True, "data": _valid_plan_dict("draft", shot_count=2, per_shot=10.0), "model_used": "m"}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)

    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, reference=_SPEC)

    assert plan["meta"]["stages"]["angles"] == {"ok": False, "skipped": "reference"}
    assert "angles" not in captured_kinds
    assert "write" in captured_kinds
    assert "critique" in captured_kinds


def test_run_director_reference_none_still_runs_angles_stage(monkeypatch):
    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        if kind == "angles":
            return {
                "ok": True,
                "data": {
                    "candidates": [{"angle": "A", "viewpoint": "v", "hook": "h", "emotional_arc": "e", "why_it_works": "w"}],
                    "chosen_index": 0, "chosen_reason": "理由",
                },
                "model_used": "m",
            }
        return {"ok": True, "data": _valid_plan_dict("draft"), "model_used": "m"}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, reference=None)
    assert plan["meta"]["stages"]["angles"]["ok"] is True


# ---------------------------------------------------------------------------
# 丸写し検出 -> 矯正リトライ
# ---------------------------------------------------------------------------

_OVERLAP_TEXT = _SPEC["transcript"]


def test_run_director_retries_once_and_replaces_plan_when_overlap_resolved(monkeypatch):
    calls = []

    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        calls.append(kind)
        if kind == "write":
            return {"ok": True, "data": _valid_plan_dict(_OVERLAP_TEXT), "model_used": "m"}
        if kind == "critique":
            return {"ok": True, "data": _valid_plan_dict(_OVERLAP_TEXT), "model_used": "m"}
        if kind == "corrective":
            return {"ok": True, "data": _valid_plan_dict("自分なりのやり方で三段階に分けて工夫した結果を紹介する回です"), "model_used": "m"}
        raise AssertionError("想定外のプロンプト種別: {}".format(kind))

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, quality="supreme", reference=_SPEC)

    assert calls.count("corrective") == 1
    assert plan["narration_script"] == "自分なりのやり方で三段階に分けて工夫した結果を紹介する回です"
    assert "reference_overlap_warning" not in plan["meta"]


def test_run_director_records_warning_when_overlap_persists_after_retry(monkeypatch):
    def fake_call_claude_json(prompt, timeout_sec=600):
        # write/critique/correctiveいずれも常に丸写しテキストを返す(矯正されない想定)。
        return {"ok": True, "data": _valid_plan_dict(_OVERLAP_TEXT, shot_count=1, per_shot=20.0), "model_used": "m"}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, quality="single", reference=_SPEC)

    assert plan["narration_script"] == _OVERLAP_TEXT
    assert plan["meta"]["reference_overlap_warning"]
    assert all(len(chunk) >= 15 for chunk in plan["meta"]["reference_overlap_warning"])


def test_run_director_skips_overlap_check_when_plan_is_rule_based(monkeypatch):
    monkeypatch.setattr(director, "call_claude_json", None)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, reference=_SPEC)
    assert plan["meta"]["source"] == "rule"
    assert "reference_overlap_warning" not in plan["meta"]


def test_run_director_no_overlap_when_narration_is_original(monkeypatch):
    def fake_call_claude_json(prompt, timeout_sec=600):
        kind = _kind(prompt)
        if kind == "critique":
            return {"ok": True, "data": _valid_plan_dict("完全にオリジナルな言い回しで書いた台本です"), "model_used": "m"}
        return {"ok": True, "data": _valid_plan_dict("完全にオリジナルな言い回しで書いた台本です"), "model_used": "m"}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=20, reference=_SPEC)
    assert "reference_overlap_warning" not in plan["meta"]


# ---------------------------------------------------------------------------
# no_llm / reference未指定の既存挙動不変
# ---------------------------------------------------------------------------

def test_run_director_no_llm_ignores_reference_and_returns_rule_based_plan():
    plan = director.run_director(
        "AIで副業を始める", config={}, target_duration_sec=20, no_llm=True, reference=_SPEC
    )
    assert plan["meta"]["source"] == "rule"
    assert "reference_overlap_warning" not in plan["meta"]
