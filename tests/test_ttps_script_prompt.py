# -*- coding: utf-8 -*-
"""TTPS（美容アフィリ・徹底的にパクって進化）台本強化ブロック統合テスト。

責務は次の4カテゴリ:
1. director._build_ttps_block の注入条件（is_ttps_mode = True のときだけ非空）と変数展開。
2. compliance の TTPS 拡張（景表法NG語追加 / パターン検査 / 効果言及検知 / is_ttps_mode）。
3. subtitles の #PR テロップと「※効果には個人差があります」注記ヘルパ。
4. plan_schema の pr_disclosure フィールド。骨検査は既存 test_r4* が担保するため、
   ここでは「TTPS 統合が既存の validate_plan と共存する」ことのみ確認する。
5. director.build_ttps_script_report のレポート雛形生成。
"""
from __future__ import annotations

from pipeline import compliance, director, plan_schema, subtitles


def _minimal_reference_spec():
    return {
        "duration_sec": 15.0,
        "cuts": [{"t": 5.0}, {"t": 10.0}],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [],
        "transcript": "毛穴ケア1週間チャレンジ、まじで変わるからやってみて欲しい。",
    }


def _minimal_plan(with_effect=False, pr_disclosure=None):
    narration_a = "普通のフックです。"
    narration_b = "普通の中盤です。"
    narration_c = "普通の締めです。"
    if with_effect:
        narration_b = "3日で毛穴に変化を実感しました。"
    plan = {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "普通のコンセプト",
        "hook": "普通のフック",
        "narration_script": narration_a + narration_b + narration_c,
        "bgm_mood": "upbeat",
        "shots": [
            {"id": "s1", "visual_prompt": "abstract bg 1", "motion_preset": "static",
             "duration_sec": 4.0, "caption_jp": "s1テロップ", "narration_jp": narration_a},
            {"id": "s2", "visual_prompt": "abstract bg 2", "motion_preset": "static",
             "duration_sec": 5.0, "caption_jp": "s2テロップ", "narration_jp": narration_b},
            {"id": "s3", "visual_prompt": "abstract bg 3", "motion_preset": "static",
             "duration_sec": 4.0, "caption_jp": "s3テロップ", "narration_jp": narration_c},
        ],
    }
    if pr_disclosure is not None:
        plan["pr_disclosure"] = pr_disclosure
    return plan


# --- 1. director._build_ttps_block の注入条件 -----------------------------------

def test_build_ttps_block_returns_empty_when_disabled():
    """cfg も product も無ければ TTPS ブロックは空文字（既存プロンプトを一切変えない）。"""
    block = director._build_ttps_block(cfg={}, product=None, reference_spec=None, skeleton=None)
    assert block == ""


def test_build_ttps_block_enabled_via_cfg_ttps_enabled_true():
    """cfg.ttps.enabled=True なら product 無しでも TTPS ブロックが注入される。"""
    cfg = {"ttps": {"enabled": True}}
    block = director._build_ttps_block(cfg=cfg, product=None, reference_spec=_minimal_reference_spec(), skeleton={"shots": []})
    assert block
    assert "TTPS" in block
    # デフォルトペルソナが差し込まれること
    assert "28歳" in block


def test_build_ttps_block_enabled_via_product():
    """product 指定があれば cfg.ttps 無しでも TTPS ブロックが注入される。"""
    block = director._build_ttps_block(
        cfg={}, product={"name": "テスト美容液", "url": "https://example.com"},
        reference_spec=_minimal_reference_spec(),
        skeleton={"shots": [{"id": "s1", "duration_sec": 3.0, "caption_in_offset_sec": 0.5,
                              "telop_style_hint": {"position": "top", "color": "white"}}]},
    )
    assert "テスト美容液" in block
    assert "https://example.com" in block
    # 参考文字起こしと構造要約が入る
    assert "毛穴ケア" in block
    assert "s1: dur=3.00s" in block


def test_build_ttps_block_respects_custom_persona():
    cfg = {"ttps": {"enabled": True, "persona": "テスト用の独自ペルソナ文言"}}
    block = director._build_ttps_block(cfg=cfg, product=None, reference_spec=None, skeleton=None)
    assert "テスト用の独自ペルソナ文言" in block
    # デフォルトペルソナは注入されない
    assert "28歳" not in block


def test_build_ttps_block_truncates_long_transcript():
    """長すぎる文字起こしは頭を切って(省略) を末尾に付ける（コスト管理）。"""
    long_transcript = "あ" * 5000
    ref = {"duration_sec": 10.0, "transcript": long_transcript, "cuts": []}
    block = director._build_ttps_block(cfg={"ttps": {"enabled": True}}, product=None,
                                          reference_spec=ref, skeleton={"shots": []})
    assert "(省略)" in block


# --- 2. compliance の TTPS 拡張 --------------------------------------------------

def test_is_ttps_mode_defaults_false():
    assert compliance.is_ttps_mode({}) is False
    assert compliance.is_ttps_mode({}, plan=_minimal_plan()) is False


def test_is_ttps_mode_true_with_cfg_flag():
    assert compliance.is_ttps_mode({"ttps": {"enabled": True}}) is True


def test_is_ttps_mode_false_when_cfg_explicitly_disabled_even_with_product():
    """cfg.ttps.enabled=False は明示的な無効化。product 有りでも False を優先する。"""
    assert compliance.is_ttps_mode({"ttps": {"enabled": False}}, product={"name": "x"}) is False


def test_is_ttps_mode_true_with_product():
    assert compliance.is_ttps_mode({}, product={"name": "テスト"}) is True


def test_build_ttps_ng_words_includes_yakkiho_and_keihyo():
    words = compliance.build_ttps_ng_words()
    # 既存 defaults
    assert "絶対稼げる" in words
    # 薬機法 NG
    assert "シミが消える" in words
    # 景表法 NG
    assert any(w.startswith("必ず") for w in words)


def test_build_ttps_ng_patterns_returns_compilable_regexes():
    import re
    patterns = compliance.build_ttps_ng_patterns()
    assert patterns
    for p in patterns:
        re.compile(p)  # コンパイル失敗しないこと


def test_check_plan_with_ttps_ng_words_detects_keihyo_word():
    plan = _minimal_plan()
    plan["shots"][1]["narration_jp"] = "この商品は絶対に効くって聞きました。"
    plan["narration_script"] = (
        plan["shots"][0]["narration_jp"] + plan["shots"][1]["narration_jp"] + plan["shots"][2]["narration_jp"]
    )
    result = compliance.check_plan(
        plan, ng_words=compliance.build_ttps_ng_words(),
        ng_patterns=compliance.build_ttps_ng_patterns(),
    )
    assert result["ok"] is False
    assert any("絶対" in v["word"] for v in result["violations"])


def test_detect_effect_mention_shot_ids_hits_effect_mentions():
    plan = _minimal_plan(with_effect=True)
    hits = compliance.detect_effect_mention_shot_ids(plan)
    assert "s2" in hits


def test_detect_effect_mention_shot_ids_returns_empty_when_no_mentions():
    plan = _minimal_plan(with_effect=False)
    hits = compliance.detect_effect_mention_shot_ids(plan)
    assert hits == []


def test_detect_effect_mention_shot_ids_checks_captions_list():
    """captions[]（複数テロップ shot）の text も検査対象に含めること。"""
    plan = _minimal_plan()
    plan["shots"][1]["captions"] = [
        {"text": "普通のテロップ"},
        {"text": "3週間でハリを実感"},
    ]
    hits = compliance.detect_effect_mention_shot_ids(plan)
    assert "s2" in hits


# --- 3. subtitles: #PR テロップと個人差注記 --------------------------------------

def test_build_pr_disclosure_piece_none_when_pr_text_empty():
    assert subtitles.build_pr_disclosure_piece(30.0, pr_text=None) is None
    assert subtitles.build_pr_disclosure_piece(30.0, pr_text="") is None


def test_build_pr_disclosure_piece_none_when_duration_zero():
    assert subtitles.build_pr_disclosure_piece(0.0, pr_text="#PR") is None


def test_build_pr_disclosure_piece_spans_full_duration_top_right_small():
    piece = subtitles.build_pr_disclosure_piece(20.0, pr_text="#PR")
    assert piece is not None
    assert piece["out_start"] == 0.0
    assert piece["out_end"] == 20.0
    assert piece["caption"] == "#PR"
    # top-right & 小さめのフォント
    assert piece["telop_style_hint"]["position"] == "top"
    assert piece["font_px_override"] < subtitles.STYLE_BASE_FONTSIZE


def test_pr_piece_generates_top_right_alignment_in_ass():
    """generate_ass に PR piece を混ぜ込んだ ASS 出力に \\an9 相当（top-right）と
    #PR 本文が含まれること（既存の generate_ass はそのまま流用）。"""
    shots = [
        {"id": "s1", "duration_sec": 3.0, "caption_jp": "本編のテロップ"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "本編のテロップ2"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    pieces.append(subtitles.build_pr_disclosure_piece(6.0, pr_text="#PR"))
    ass = subtitles.generate_ass(pieces)
    assert "#PR" in ass
    # top-right = \an9 or \an8 のいずれか（position="top" → \an8 と対応する仕様）。
    # subtitles._HINT_POSITION_ALIGNMENT で position=top → 8 に写像されるため
    # \an8 を確認する（右寄せまでは求めない＝\an8 で top-center）。
    assert "\\an8" in ass


def test_build_variance_note_pieces_only_for_hit_shots():
    shots = [
        {"id": "s1", "duration_sec": 3.0, "caption_jp": "普通"},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": "普通"},
        {"id": "s3", "duration_sec": 3.0, "caption_jp": "普通"},
    ]
    pieces = subtitles.build_variance_note_pieces_for_shots(shots, hit_shot_ids=["s2"])
    assert len(pieces) == 1
    p = pieces[0]
    assert p["caption"] == compliance.INDIVIDUAL_VARIANCE_NOTE
    # s2 の表示区間 = [3.0, 7.0]。末尾 <=1.2s に載る。
    assert 3.0 <= p["out_start"] <= 7.0
    assert p["out_end"] == 7.0
    # codex-review P1: 変性注記は PR (top) と分離するため middle(=画面中央) に置く。
    assert p["telop_style_hint"]["position"] == "middle"


def test_pr_and_variance_note_do_not_share_alignment():
    """PR(上) / メイン(下) / 変性注記(中央) の3層分離を保証する。
    codex-review P1 対応の回帰防止テスト。"""
    shots = [
        {"id": "s1", "duration_sec": 3.0, "caption_jp": "普通のテロップ"},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": "3日で実感しました"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    pieces.extend(subtitles.build_variance_note_pieces_for_shots(shots, hit_shot_ids=["s2"]))
    pr = subtitles.build_pr_disclosure_piece(7.0, pr_text="#PR")
    if pr:
        pieces.append(pr)
    ass = subtitles.generate_ass(pieces)
    # PR = \an8 (top-center) / 変性注記 = \an5 (middle-center) が両方登場していること。
    assert "\\an8" in ass  # PR
    assert "\\an5" in ass  # variance note
    # 変性注記行が \an5 を持っていることを厳密に確認
    variance_line = next(l for l in ass.splitlines() if compliance.INDIVIDUAL_VARIANCE_NOTE in l)
    assert "\\an5" in variance_line
    pr_line = next(l for l in ass.splitlines() if "#PR" in l)
    assert "\\an8" in pr_line


def test_build_variance_note_pieces_empty_when_no_hits():
    shots = [{"id": "s1", "duration_sec": 3.0, "caption_jp": "x"}]
    assert subtitles.build_variance_note_pieces_for_shots(shots, hit_shot_ids=[]) == []


# --- 4. plan_schema: pr_disclosure 任意フィールド --------------------------------

def test_plan_schema_accepts_pr_disclosure_field():
    plan = _minimal_plan(pr_disclosure="#PR")
    ok, errors, norm = plan_schema.validate_plan(plan, target_duration_sec=13.0)
    assert ok, errors
    assert norm.get("pr_disclosure") == "#PR"


def test_plan_schema_pr_disclosure_none_or_empty_is_dropped():
    """None / 空文字は保存されない（既存 plan の後方互換）。"""
    plan = _minimal_plan(pr_disclosure="")
    ok, errors, norm = plan_schema.validate_plan(plan, target_duration_sec=13.0)
    assert ok, errors
    assert "pr_disclosure" not in norm


# --- 5. director.build_ttps_script_report のレポート雛形 ------------------------

def test_build_ttps_script_report_includes_all_sections():
    plan = _minimal_plan(with_effect=True)
    md = director.build_ttps_script_report(
        plan, reference_spec=_minimal_reference_spec(), cfg={},
        product={"name": "テスト美容液"},
    )
    # 4 節が全部あること
    assert "## 1. 参考動画の文字起こし" in md
    assert "## 2. 完成台本" in md
    assert "## 3. 映像視点" in md
    assert "## 4. その他解説" in md
    # コンプラチェック節と効果言及shot（s2）
    assert "コンプラチェック" in md
    assert "s2" in md
    # 訴求商品名
    assert "テスト美容液" in md


def test_build_ttps_script_report_reports_ng_word_violation():
    plan = _minimal_plan()
    plan["shots"][1]["narration_jp"] = "この商品は必ず効くと聞きました。"
    plan["narration_script"] = (
        plan["shots"][0]["narration_jp"] + plan["shots"][1]["narration_jp"] + plan["shots"][2]["narration_jp"]
    )
    md = director.build_ttps_script_report(plan, reference_spec=None, cfg={}, product=None)
    assert "NG:" in md
    assert "必ず" in md


# --- 6. 骨不変ガード（TTPS 統合は既存の骨検査を破らない） -----------------------

def test_ttps_block_does_not_touch_skeleton_validate():
    """TTPS ブロックは prompt へ concat されるだけで、skeleton 一致検査は既存ロジックを
    そのまま流用する。skeleton と plan が一致していれば TTPS 有無で結果は変わらない。"""
    # skeleton の shot 全てに caption_in_offset_sec を付けて、plan 側の caption_jp と一致させる。
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 4.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 4.0},
            {"id": "s2", "duration_sec": 5.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 5.0},
            {"id": "s3", "duration_sec": 4.0, "motion_preset": "static",
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 4.0},
        ],
        "sfx_plan": [],
        "hook_end_shot_id": None,
        "cta_start_shot_id": None,
    }
    plan = _minimal_plan()
    # plan 側にも caption offset を付ける（skeleton と一致必須）
    for s in plan["shots"]:
        s["caption_in_offset_sec"] = 0.0
        s["caption_out_offset_sec"] = s["duration_sec"]
    errs = director._validate_plan_matches_skeleton(plan, skeleton, target_duration_sec=13.0)
    assert errs == [], errs
