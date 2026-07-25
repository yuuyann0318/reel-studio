# -*- coding: utf-8 -*-
"""TTP「複製機化」P0 の機械検証（診断 /tmp/ttp_gap_diagnosis.md 対応）。

対象: director を「参考の複製機」に引き上げる改修の骨検査。
  P0-1a テロップ実文言(進捗ゲージ)の複製
  P0-1b カット非集約(1参考カット=1shot)
  P0-2  narration_mode(absent) 推定 + narration 空許容
  P0-3  BGM選定の参考準拠(bpm/mood_guess)
  P0-4  SFX実配置(sfx_plan 非空)
"""
import json
import os

import pytest

from pipeline import director, bgm_library, plan_schema

_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "assets", "reference_cache",
    "ff981f340605621411db6d231a19d3160d1553e8_v2.json",
)


def _load_spec():
    with open(_CACHE, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def spec():
    return _load_spec()


# --- P0-1b: カット非集約 ------------------------------------------------------

def test_one_cut_one_shot_no_aggregation(spec):
    """参考の cuts=8 → 境界9 → 9 shot。scene集約(4シーン化)されないこと。"""
    sk = director.build_shot_skeleton(spec, 18.9, max_shot_sec=None, min_shot_sec=0.5)
    assert len(sk["shots"]) == 9
    # 合計尺は target ぴったり（P2-7 尺ハード制約）
    assert abs(sum(s["duration_sec"] for s in sk["shots"]) - 18.9) <= 0.1


# --- P0-1a: テロップ実文言(進捗ゲージ)の複製 ---------------------------------

def test_skeleton_carries_reference_telop_text(spec):
    sk = director.build_shot_skeleton(spec, 18.9, min_shot_sec=0.5)
    all_ref = []
    for s in sk["shots"]:
        if s.get("ref_caption_text"):
            all_ref.append(s["ref_caption_text"])
        for slot in s.get("caption_slots") or []:
            if slot.get("ref_text"):
                all_ref.append(slot["ref_text"])
    joined = "\n".join(all_ref)
    # 進捗ゲージ文言が skeleton に確実に運ばれている
    assert "10%/100%" in joined
    assert "100%/100%" in joined


def test_gauge_tokens_extraction():
    toks = director._gauge_tokens("10%/100%\nSNSでバズってる美容液")
    assert "10%/100%" in toks
    # 数字を含まない短行は verbatim 対象外
    assert "SNSでバズってる美容液" not in toks


def test_gauge_tokens_excludes_facts_and_prices():
    """codex-review High: 価格/期間/主張(数字を含む文章)は verbatim 強制しない。"""
    assert director._gauge_tokens("30日で改善") == []
    assert director._gauge_tokens("¥1980") == []
    assert director._gauge_tokens("3週間で二度見") == []
    # 純ゲージだけ拾う
    assert director._gauge_tokens("100%") == ["100%"]
    assert director._gauge_tokens("1/2") == ["1/2"]


def test_validate_flags_missing_gauge_and_passes_when_kept():
    """進捗ゲージを落とした plan は不合格、複製した plan は合格。"""
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 2.0,
             "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 2.0,
             "ref_caption_text": "10%/100%\nバズってる美容液"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    base_shot = {
        "id": "s1", "duration_sec": 2.0,
        "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 2.0,
        "visual_prompt": "x", "narration_jp": "テスト",
    }
    # ゲージを落とす → エラー
    bad = {"shots": [dict(base_shot, caption_jp="話題のアイテム")]}
    errs = director._validate_plan_matches_skeleton(bad, skeleton, 2.0)
    assert any("10%/100%" in e for e in errs)
    # ゲージを複製 → ゲージ由来のエラーは無い
    good = {"shots": [dict(base_shot, caption_jp="10%/100% 話題のアイテム")]}
    errs2 = director._validate_plan_matches_skeleton(good, skeleton, 2.0)
    assert not any("10%/100%" in e for e in errs2)


# --- P0-2: narration_mode -----------------------------------------------------

def test_infer_narration_mode_absent(spec):
    # 実キャッシュ: transcript空 + ASR失敗warning + telops9 → absent
    assert director._infer_narration_mode(spec) == "absent"
    sk = director.build_shot_skeleton(spec, 18.9, min_shot_sec=0.5)
    assert sk["narration_mode"] == "absent"


def test_infer_narration_mode_present():
    spec = {"transcript": "これは実際の文字起こしです。", "telops": [], "warnings": [], "bgm": {}}
    assert director._infer_narration_mode(spec) == "present"


def test_infer_narration_mode_unknown():
    spec = {"transcript": "", "telops": [], "warnings": [], "bgm": {"present": False}}
    assert director._infer_narration_mode(spec) == "unknown"


def test_plan_schema_allows_empty_narration_when_absent():
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h",
        "narration_mode": "absent", "narration_script": "",
        "bgm_mood": "upbeat",
        "shots": [{"id": "s1", "visual_prompt": "x", "duration_sec": 3.0,
                   "caption_jp": "10%", "narration_jp": ""}],
    }
    ok, errs, norm = plan_schema.validate_plan(plan, target_duration_sec=3.0, target_tolerance_sec=8.0)
    assert ok, errs
    assert norm["narration_mode"] == "absent"
    assert norm["narration_script"] == ""


def test_plan_schema_absent_force_empties_leftover_narration():
    """codex-review High: absent なのに narration が残っていたら空へ正規化する。"""
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h",
        "narration_mode": "absent",
        "narration_script": "勝手に足したナレーション",  # 残骸
        "bgm_mood": "upbeat",
        "shots": [{"id": "s1", "visual_prompt": "x", "duration_sec": 3.0,
                   "caption_jp": "10%", "narration_jp": "残骸ナレ"}],
    }
    ok, errs, norm = plan_schema.validate_plan(plan, target_duration_sec=3.0)
    assert ok, errs
    assert norm["narration_script"] == ""
    assert norm["shots"][0]["narration_jp"] == ""


def test_pick_bgm_bpm_priority_over_recency():
    """codex-review Medium: bpm 近傍を直近回避より優先する。"""
    m = bgm_library.load_manifest()
    # upbeat_bright_01(128) を直近使用済みにしても、129 の参考なら bpm 優先で選ばれ得る
    hist = {"recent": [{"file": "library/upbeat_house_02.m4a", "mood": "upbeat", "project_id": "p0"},
                       {"file": "library/upbeat_pop_03.m4a", "mood": "upbeat", "project_id": "p0"}]}
    chosen = bgm_library.pick_bgm("upbeat", seed=3, manifest=m, history=hist, target_bpm=129.2)
    # 129±15 内は 128 のみ → 直近除外で pool が空になっても候補へフォールバックして 128 を返す
    assert abs(float(chosen["bpm"]) - 129.2) <= 15.0


def test_pick_bgm_handles_non_finite_target():
    m = bgm_library.load_manifest()
    chosen = bgm_library.pick_bgm("upbeat", seed=1, manifest=m, history={"recent": []},
                                  target_bpm=float("inf"))
    assert chosen is not None  # inf は無視され通常選曲にフォールバック


def test_plan_schema_requires_narration_when_present():
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h",
        "narration_script": "",  # present(既定)で空 → 不合格
        "bgm_mood": "upbeat",
        "shots": [{"id": "s1", "visual_prompt": "x", "duration_sec": 3.0, "caption_jp": "a"}],
    }
    ok, errs, _ = plan_schema.validate_plan(plan, target_duration_sec=3.0)
    assert not ok


# --- P0-3: BGM選定の参考準拠 --------------------------------------------------

def test_reference_bgm_mood_and_bpm(spec):
    sk = director.build_shot_skeleton(spec, 18.9, min_shot_sec=0.5)
    rb = sk["reference_bgm"]
    assert rb["library_mood"] == "upbeat"
    assert abs(rb["bpm"] - 129.2) < 0.5


def test_pick_bgm_prefers_reference_bpm():
    """129BPM/upbeat の参考 → 90BPM の calm が選ばれず、±15BPM の曲が選ばれる。"""
    m = bgm_library.load_manifest()
    hist = {"recent": []}
    chosen = bgm_library.pick_bgm("upbeat", seed=7, manifest=m, history=hist, target_bpm=129.2)
    assert chosen is not None
    assert chosen.get("mood") == "upbeat"
    assert abs(float(chosen["bpm"]) - 129.2) <= 15.0


def test_map_mood_guess():
    assert director._map_mood_guess_to_library_mood("upbeat pop") == "upbeat"
    assert director._map_mood_guess_to_library_mood("calm ambient") == "calm"
    assert director._map_mood_guess_to_library_mood("") is None


# --- P0-4: SFX実配置 ----------------------------------------------------------

def test_sfx_plan_nonempty_from_events(spec):
    """参考 sfx_events 71件 → sfx_plan は非空。"""
    assert len(spec.get("sfx_events") or []) >= 50
    sk = director.build_shot_skeleton(spec, 18.9, min_shot_sec=0.5)
    assert len(sk.get("sfx_plan") or []) >= 1
