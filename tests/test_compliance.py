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
