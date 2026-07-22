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


def test_check_plan_detects_competitor_brand_name_via_custom_dict():
    """特定の個人名・SNSアカウント名は DEFAULT_NG_WORDS には含めない設計だが、
    呼び出し側 (ng_words) or ローカル辞書 (config.local/ngwords.json) 経由で
    渡された文言が本文にあれば violations として検出される。"""
    result = compliance.check_plan(
        _plan(narration="サンプル花子さんのアカウントを参考にしました。"),
        ng_words=compliance.DEFAULT_NG_WORDS + ["サンプル花子"],
    )
    assert result["ok"] is False
    assert any(v["word"] == "サンプル花子" for v in result["violations"])


def test_check_plan_default_ng_words_do_not_contain_real_personal_names():
    """DEFAULT_NG_WORDS には特定個人名・SNSアカウント名を tracked コードとして含めない
    (公開リポジトリで実名を露出させないための恒久ルール)。"""
    for w in compliance.DEFAULT_NG_WORDS:
        assert not w.startswith("@"), "SNSアカウントハンドルは DEFAULT_NG_WORDS に置かない"
    # 非空・全て文字列であることは維持
    assert all(isinstance(w, str) and w for w in compliance.DEFAULT_NG_WORDS)


def test_load_local_ng_words_absent_returns_empty(tmp_path, monkeypatch):
    """`config.local/ngwords.json` が存在しない環境では空リストを返す
    (tracked コードだけで壊れず動作すること)。"""
    monkeypatch.setattr(compliance, "_LOCAL_NG_WORDS_PATH", tmp_path / "does_not_exist.json")
    assert compliance._load_local_ng_words() == []
    # get_effective_ng_words も DEFAULT のみを返す
    assert compliance.get_effective_ng_words() == list(compliance.DEFAULT_NG_WORDS)


def test_load_local_ng_words_merges_into_effective_list(tmp_path, monkeypatch):
    """ローカル辞書に置いた実名は get_effective_ng_words() 経由でマージされ、
    check_plan() 既定挙動でも検出される。"""
    dict_path = tmp_path / "ngwords.json"
    dict_path.write_text('{"ng_words": ["サンプル花子"]}', encoding="utf-8")
    monkeypatch.setattr(compliance, "_LOCAL_NG_WORDS_PATH", dict_path)
    effective = compliance.get_effective_ng_words()
    assert "サンプル花子" in effective
    # check_plan の既定パラメータでも検出される
    result = compliance.check_plan(_plan(narration="サンプル花子さんの投稿。"))
    assert result["ok"] is False
    assert any(v["word"] == "サンプル花子" for v in result["violations"])


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


# --- shots[].narration_jp（音声主導タイミング同期モードの断片テキスト）の検査漏れ修正 -------

def _plan_with_narration_jp(narration_jp, narration_script="普通のナレーションです。"):
    return {
        "concept": "普通のコンセプト",
        "hook": "普通のフック",
        "narration_script": narration_script,
        "shots": [
            {"id": "s1", "caption_jp": "普通のキャプション", "visual_prompt": "abstract bg", "narration_jp": narration_jp},
        ],
    }


def test_check_plan_detects_ng_word_in_shot_narration_jp():
    """narration_script(全文)にはNGワードが無くても、shots[].narration_jp(断片)にあれば検出する
    （音声主導タイミング同期モードで実際にTTS音声化される文言のため検査漏れがあってはならない）。"""
    result = compliance.check_plan(_plan_with_narration_jp("このやり方なら絶対稼げるようになります。"))
    assert result["ok"] is False
    assert any(v["word"] == "絶対稼げる" and v["field"] == "shots[s1].narration_jp" for v in result["violations"])


def test_check_plan_narration_jp_absent_is_unaffected():
    """narration_jpキー自体が無いshot（従来の全文方式のみのplan）の挙動は不変。"""
    plan = {
        "concept": "c", "hook": "h", "narration_script": "普通のナレーションです。",
        "shots": [{"id": "s1", "caption_jp": "普通のキャプション", "visual_prompt": "abstract bg"}],
    }
    result = compliance.check_plan(plan)
    assert result["ok"] is True
    assert result["violations"] == []
