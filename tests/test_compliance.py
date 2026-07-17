# -*- coding: utf-8 -*-
from pipeline import compliance


def _plan(narration="普通のナレーションです。", captions=None):
    return {
        "concept": "普通のコンセプト",
        "hook": "普通のフック",
        "narration_script": narration,
        "shots": [
            {"id": "s1", "caption_jp": (captions[0] if captions else "普通のキャプション"), "visual_prompt": "abstract bg"},
        ],
    }


def test_check_plan_ok_no_violations():
    result = compliance.check_plan(_plan())
    assert result["ok"] is True
    assert result["violations"] == []


def test_check_plan_detects_keihyo_ng_word_in_narration():
    result = compliance.check_plan(_plan(narration="このやり方なら絶対稼げるようになります。"))
    assert result["ok"] is False
    assert any(v["word"] == "絶対稼げる" for v in result["violations"])


def test_check_plan_detects_competitor_brand_name():
    result = compliance.check_plan(_plan(narration="みおさんのアカウントを参考にしました。"))
    assert result["ok"] is False
    assert any(v["word"] == "みお" for v in result["violations"])


def test_check_plan_detects_ng_word_in_caption():
    result = compliance.check_plan(_plan(captions=["100%成功する方法"]))
    assert result["ok"] is False
    assert any(v["word"] == "100%成功" for v in result["violations"])


def test_check_plan_warns_on_subscription_without_cancellation_hint():
    result = compliance.check_plan(_plan(narration="月額プランに登録すると使い放題になります。"))
    assert result["ok"] is True
    assert len(result["warnings"]) == 1


def test_check_plan_no_warning_when_cancellation_mentioned():
    result = compliance.check_plan(_plan(narration="月額プランはいつでも解約できます。"))
    assert result["warnings"] == []


def test_check_plan_custom_ng_words():
    result = compliance.check_plan(_plan(narration="カスタムNGワードを含みます。"), ng_words=["カスタムNGワード"])
    assert result["ok"] is False
    assert result["violations"][0]["word"] == "カスタムNGワード"


# --- 美容系薬機法(ng_patterns)拡張 -------------------------------------------

def test_check_plan_default_mode_unaffected_by_beauty_yakkiho_lists():
    """ng_words/ng_patterns未指定時、BEAUTY_YAKKIHO系は一切混入せず既定挙動は完全不変。"""
    result = compliance.check_plan(_plan(narration="このクリームで毎日のケアが楽しくなります。"))
    assert result["ok"] is True
    assert result["violations"] == []
    for w in compliance.BEAUTY_YAKKIHO_NG_WORDS:
        assert w not in compliance.DEFAULT_NG_WORDS


def test_check_plan_detects_beauty_yakkiho_ng_word_when_explicitly_passed():
    result = compliance.check_plan(
        _plan(narration="このクリームを塗るだけで痩せることが証明されています。"),
        ng_words=compliance.DEFAULT_NG_WORDS + compliance.BEAUTY_YAKKIHO_NG_WORDS,
    )
    assert result["ok"] is False
    assert any(v["word"] == "塗るだけで痩せる" for v in result["violations"])


def test_check_plan_detects_beauty_yakkiho_ng_pattern_when_explicitly_passed():
    result = compliance.check_plan(
        _plan(narration="使い続けたら頑固なシミがみるみる消えました。"),
        ng_words=[],
        ng_patterns=compliance.BEAUTY_YAKKIHO_NG_PATTERNS,
    )
    assert result["ok"] is False
    assert any("消え" in v["word"] for v in result["violations"])


def test_check_plan_ng_patterns_none_by_default_no_pattern_matching():
    """ng_patterns=None(既定)なら、パターンにマッチする文言があってもng_wordsに無ければ検出しない
    （後方互換: 既存呼び出し側の挙動を一切変えない）。"""
    result = compliance.check_plan(_plan(narration="頑固なシミがみるみる消えました。"))
    assert result["ok"] is True
    assert result["violations"] == []


def test_check_plan_ng_patterns_accepts_precompiled_pattern():
    import re

    result = compliance.check_plan(
        _plan(narration="必ず痩せると評判です。"),
        ng_words=[],
        ng_patterns=[re.compile(r"必ず痩せ")],
    )
    assert result["ok"] is False
    assert result["violations"][0]["word"] == "必ず痩せ"
