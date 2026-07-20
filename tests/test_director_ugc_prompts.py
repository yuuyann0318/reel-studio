# -*- coding: utf-8 -*-
"""UGCネイティブ化のプロンプト回帰テスト。

生成動画が「広告っぽい」問題への対策として、writer(default/vertical_hook)と
critiqueの各プロンプトに「広告っぽさの徹底排除(UGCネイティブ)」セクションを追加し、
defaultのcaption=見出し化という矛盾文言を「narration_jpとほぼ同文」優先へ書き換えた。
"""
import os

from pipeline import director

_PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pipeline", "prompts")

_UGC_HEADER = "# 広告っぽさの徹底排除(UGCネイティブ"
# UGCセクションに必ず含まれる代表フレーズ（禁止広告構文・スマホ生活感）。
_UGC_MARKERS = ["友達に送る早口の体験談", "smartphone footage", "advertisement"]


def _read(name):
    with open(os.path.join(_PROMPT_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_default_prompt_has_ugc_section():
    text = _read("director_prompt.txt")
    assert _UGC_HEADER in text
    for marker in _UGC_MARKERS:
        assert marker in text


def test_vertical_hook_prompt_has_ugc_section_with_vertical_caption_note():
    text = _read("director_prompt_vertical_hook.txt")
    assert _UGC_HEADER in text
    for marker in _UGC_MARKERS:
        assert marker in text
    # 縦書きは短文見出しのままにする注記が入っていること。
    assert "縦書きは13字目安の短文のまま" in text


def test_critique_prompt_has_ugc_review_section():
    text = _read("critique_prompt.txt")
    assert _UGC_HEADER in text
    # レビュー観点としても追加されていること。
    assert "広告っぽさの排除(UGCネイティブ)" in text


def test_default_prompt_removed_headline_caption_conflict():
    """defaultの「caption=要点の見出し化/丸写しでなく」という矛盾文言が除去されていること。"""
    text = _read("director_prompt.txt")
    assert "要点の見出し化" not in text
    assert "丸写しではなく" not in text
    # 新方針: caption_jp は narration_jp とほぼ同文（全文テロップ）。
    assert "narration_jpとほぼ同文" in text


def test_built_director_prompts_include_ugc_section():
    """director.build_director_prompt 経由でもUGCセクションが配線されていること。"""
    default_prompt = director.build_director_prompt("テーマ", 20, style="default")
    vh_prompt = director.build_director_prompt("テーマ", 15, style="vertical_hook")
    assert _UGC_HEADER in default_prompt
    assert _UGC_HEADER in vh_prompt
