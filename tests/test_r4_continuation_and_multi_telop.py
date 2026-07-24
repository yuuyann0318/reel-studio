# -*- coding: utf-8 -*-
"""R4: 分割透過化(continuation_of) と 複数テロップ(captions[]) の単体テスト。

背景（R3.5→R4 の申し送り）:
  1. クレジット意識分割(build_shot_skeleton の max_shot_sec)が mock でも発動し、
     参考の1shotを実装都合で2分割していた → cut_match が構造的に上限貼り付き。
     R4: backend パラメータで gate。mock 時は分割せず、higgsfield 時のみ分割する。
     分割時も後続断片に continuation_of を張り、fidelity 側で「参考に無いカット」として
     計上しない。
  2. 1shot=1telop 制約で同 shot の 2枚目以降テロップが捨てられていた → telop_iou の
     denom がズレて構造的に目減り。R4: caption_slots[] を skeleton で、captions[] を
     plan で持ち、複数テロップを subtitles / premiere / fidelity まで貫通させる。
"""
from __future__ import annotations

import json
import os

import pytest

from pipeline import director, plan_schema, subtitles
from qa import fidelity


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# KR1: continuation_of の透過化
# ---------------------------------------------------------------------------

def test_split_sets_continuation_of_on_non_head_fragments(real_spec):
    """max_shot_sec 分割で 2 番目以降の断片に continuation_of が入る。"""
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    ids = [s["id"] for s in sk["shots"]]
    cofs = {s["id"]: s.get("continuation_of") for s in sk["shots"]}
    # 分割された shot が存在すること（fixture の s3 は 14.8s なので 10s で分割される）
    assert any(v is not None for v in cofs.values()), \
        "少なくとも 1 shot は continuation_of を持つはず"
    # continuation_of で指される親 id は同一 skeleton の他の shot id
    for sid, cof in cofs.items():
        if cof is not None:
            assert cof in ids
            # continuation_of は自分より前の shot を指す
            assert ids.index(cof) < ids.index(sid)


def test_no_continuation_when_no_split(real_spec):
    """max_shot_sec=None のときは continuation_of が付かない（後方互換）。"""
    sk = director.build_shot_skeleton(real_spec, 30.0, max_shot_sec=None)
    for s in sk["shots"]:
        assert "continuation_of" not in s


def test_run_director_gates_max_shot_sec_by_backend(real_spec, monkeypatch):
    """R4: backend=mock なら config.higgsfield.max_credits_per_shot があっても分割しない。
    backend=higgsfield なら従来どおり分割する。backend=None は従来挙動（後方互換）。"""
    captured = {}
    real_build = director.build_shot_skeleton

    def _spy(spec, target, max_shot_sec=None, **kwargs):
        captured["max_shot_sec"] = max_shot_sec
        return real_build(spec, target, max_shot_sec=max_shot_sec, **kwargs)

    monkeypatch.setattr(director, "build_shot_skeleton", _spy)

    def _fake_attempt(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                      trace=None, skeleton=None, reference=None):
        if trace is not None:
            trace["last_model_used"] = "test-model"
        if not skeleton:
            return None
        return {
            "version": 2, "meta": {"source": "ai"},
            "concept": "c", "hook": "h", "narration_script": "n",
            "shots": [], "bgm_mood": "upbeat",
        }

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    # backend=mock → 分割しない（max_shot_sec=None）
    try:
        director.run_director(
            "theme", config={"higgsfield": {"max_credits_per_shot": 10}},
            target_duration_sec=30.0, reference=real_spec, quality="single",
            backend="mock",
        )
    except Exception:
        pass
    assert captured.get("max_shot_sec") is None, \
        "backend=mock では max_shot_sec が抑止されるはず"

    # backend=higgsfield → 分割する
    captured.clear()
    try:
        director.run_director(
            "theme", config={"higgsfield": {"max_credits_per_shot": 10}},
            target_duration_sec=30.0, reference=real_spec, quality="single",
            backend="higgsfield",
        )
    except Exception:
        pass
    assert captured.get("max_shot_sec") == 10.0, \
        "backend=higgsfield では max_shot_sec が有効"

    # backend=None（後方互換）→ 従来どおり分割
    captured.clear()
    try:
        director.run_director(
            "theme", config={"higgsfield": {"max_credits_per_shot": 10}},
            target_duration_sec=30.0, reference=real_spec, quality="single",
        )
    except Exception:
        pass
    assert captured.get("max_shot_sec") == 10.0


def test_fidelity_cut_match_excludes_continuation_boundaries(real_spec):
    """継続断片の境界は cut_match の分母から除外される。同じ論理構成なら分割ありでも同スコア。"""
    # 分割なし plan
    sk_nosplit = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=None)
    plan_nosplit = _skeleton_to_stub_plan(sk_nosplit)
    fid_nosplit = fidelity.compute_fidelity(real_spec, plan_nosplit)
    # 分割あり plan
    sk_split = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    plan_split = _skeleton_to_stub_plan(sk_split)
    fid_split = fidelity.compute_fidelity(real_spec, plan_split)
    # cut_match: 分割で内部境界を計上しない → 分割ありでも分割なしと同等の値になる
    assert fid_split["summary"]["cut_match"] >= fid_nosplit["summary"]["cut_match"] - 1e-6, (
        "continuation_of 除外で cut_match は劣化しないはず "
        "(nosplit={}, split={})".format(fid_nosplit["summary"]["cut_match"],
                                          fid_split["summary"]["cut_match"])
    )


def test_beat_alignment_ignores_continuation_boundaries(real_spec):
    """beat_alignment も continuation 境界を分母から除外する。"""
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    plan = _skeleton_to_stub_plan(sk)
    # 単純な beat_times を仕込み
    spec = dict(real_spec)
    spec["music"] = {"beat_times": [i * 0.5 for i in range(120)], "confidence": 0.9}
    result = fidelity._beat_alignment(spec, plan, tol_sec=0.5)
    # 内部境界数 == 分割除外後の shot 数 - 1
    non_cont_count = sum(1 for i, s in enumerate(plan["shots"])
                          if i > 0 and not s.get("continuation_of"))
    # boundaries は「次shotが continuation でない」ものだけ → cont除外後の shot数-1
    logical_shot_count = sum(1 for s in plan["shots"] if not s.get("continuation_of"))
    assert result["boundaries"] == logical_shot_count - 1


# ---------------------------------------------------------------------------
# KR2: 複数テロップ対応
# ---------------------------------------------------------------------------

def test_multi_telops_kept_up_to_max_per_shot():
    """_map_telops_to_shots が同一 shot 内の複数 telop を最大 3 件まで保持する。"""
    # 1 shot 内に 4 telops
    telops = [
        {"start": 0.0, "end": 1.0, "position": "top", "confidence": 0.9,
         "style": {"color": "yellow", "size_class": "large"}},
        {"start": 1.5, "end": 2.0, "position": "center", "confidence": 0.8,
         "style": {"color": "white", "size_class": "med"}},
        {"start": 2.2, "end": 2.6, "position": "bottom", "confidence": 0.7,
         "style": {"color": "pink", "size_class": "small"}},
        {"start": 2.8, "end": 3.0, "position": "top", "confidence": 0.3,
         "style": {"color": "red", "size_class": "small"}},
    ]
    result = director._map_telops_to_shots(
        telops, boundaries=[0.0, 3.0], shot_ids=["s1"], scaled_durations=[3.0], ref_duration=3.0,
    )
    assert "s1" in result
    captions = result["s1"]["captions"]
    assert len(captions) == director._MAX_TELOPS_PER_SHOT == 3
    # confidence 上位 3 件が採用され、時系列順で並ぶ
    starts = [c["caption_in_offset_sec"] for c in captions]
    assert starts == sorted(starts)


def test_skeleton_emits_caption_slots_only_for_multi_telop_shots():
    """skeleton は複数テロップ shot だけに caption_slots[] を載せ、単一 shot には出さない（後方互換）。"""
    spec = {
        "duration_sec": 6.0,
        "cuts": [{"t": 3.0}],
        "telops": [
            {"start": 0.0, "end": 1.0, "position": "top", "confidence": 0.9,
             "style": {"color": "yellow", "size_class": "large"}},
            {"start": 1.5, "end": 2.5, "position": "bottom", "confidence": 0.8,
             "style": {"color": "white", "size_class": "med"}},
            {"start": 3.5, "end": 5.5, "position": "top", "confidence": 0.7,
             "style": {"color": "pink", "size_class": "small"}},
        ],
        "shots_ref": [
            {"start": 0.0, "end": 3.0, "camera_move": "static"},
            {"start": 3.0, "end": 6.0, "camera_move": "static"},
        ],
    }
    sk = director.build_shot_skeleton(spec, 6.0)
    # s1 は 2 telops → caption_slots[] 有り
    s1 = next(s for s in sk["shots"] if s["id"] == "s1")
    assert isinstance(s1.get("caption_slots"), list)
    assert len(s1["caption_slots"]) == 2
    # 先頭は legacy にもコピーされている
    assert "caption_in_offset_sec" in s1
    # s2 は 1 telop → caption_slots[] 無し、legacy のみ
    s2 = next(s for s in sk["shots"] if s["id"] == "s2")
    assert "caption_slots" not in s2
    assert "caption_in_offset_sec" in s2


def test_validate_plan_matches_skeleton_requires_captions_length():
    """skeleton に caption_slots[] があれば plan は同数の captions[] を必須。"""
    skeleton = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0,
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
             "caption_slots": [
                 {"caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                 {"caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
             ],
             "motion_preset": "static"},
        ],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    plan_bad = {
        "shots": [{"id": "s1", "duration_sec": 5.0, "caption_jp": "one",
                    "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                    "motion_preset": "static"}],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    errs = director._validate_plan_matches_skeleton(plan_bad, skeleton, 5.0)
    assert any("captions 件数" in e for e in errs)

    plan_good = {
        "shots": [{"id": "s1", "duration_sec": 5.0, "caption_jp": "one",
                    "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
                    "captions": [
                        {"text": "テロップ1", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                        {"text": "テロップ2", "caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
                    ],
                    "motion_preset": "static"}],
        "sfx_plan": [], "hook_end_shot_id": None, "cta_start_shot_id": None,
    }
    errs = director._validate_plan_matches_skeleton(plan_good, skeleton, 5.0)
    assert not any("captions" in e for e in errs)


def test_plan_schema_accepts_captions_array():
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "bgm_mood": "upbeat",
        "shots": [
            {"id": "s1", "visual_prompt": "vp", "motion_preset": "static",
             "duration_sec": 5.0, "caption_jp": "one",
             "captions": [
                 {"text": "テロップ1", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                 {"text": "テロップ2", "caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
             ]}
        ],
    }
    ok, errs, norm = plan_schema.validate_plan(plan, target_duration_sec=5.0)
    assert ok, errs
    assert norm["shots"][0]["captions"][0]["text"] == "テロップ1"
    assert norm["shots"][0]["captions"][1]["text"] == "テロップ2"


def test_plan_schema_accepts_continuation_of():
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "bgm_mood": "upbeat",
        "shots": [
            {"id": "s1", "visual_prompt": "vp1", "motion_preset": "static",
             "duration_sec": 5.0, "caption_jp": "one"},
            {"id": "s2", "visual_prompt": "vp1", "motion_preset": "static",
             "duration_sec": 5.0, "caption_jp": "", "continuation_of": "s1"},
        ],
    }
    ok, errs, norm = plan_schema.validate_plan(plan, target_duration_sec=10.0)
    assert ok, errs
    assert norm["shots"][1]["continuation_of"] == "s1"


def test_plan_schema_rejects_invalid_continuation_of():
    plan = {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "bgm_mood": "upbeat",
        "shots": [
            {"id": "s1", "visual_prompt": "vp1", "motion_preset": "static",
             "duration_sec": 5.0, "caption_jp": "one",
             "continuation_of": "s_not_exist"},
        ],
    }
    ok, errs, _ = plan_schema.validate_plan(plan, target_duration_sec=5.0)
    assert not ok
    assert any("continuation_of" in e for e in errs)


def test_subtitles_build_pieces_from_captions_array():
    """build_telop_pieces_from_shots が captions[] を複数 piece として展開する。"""
    shots = [
        {"id": "s1", "duration_sec": 5.0, "caption_jp": "one",
         "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5,
         "captions": [
             {"text": "テロップ1", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
             {"text": "テロップ2", "caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
         ]},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 2
    assert pieces[0]["caption"] == "テロップ1"
    assert pieces[1]["caption"] == "テロップ2"
    assert pieces[0]["out_start"] == pytest.approx(0.5)
    assert pieces[1]["out_start"] == pytest.approx(2.5)


def test_remap_telops_by_split_distributes_captions_across_fragments():
    """R4 codex-review P1 修正: 複数 telop が旧 shot に載っている場合、start が属する断片へ振り分ける
    （後半 telop を先頭断片に潰さない）。"""
    # 旧 shot s1 (dur 14.8s) に telop2枚: 2s と 12s → 分割で断片は先頭 10s / 後半 4.8s
    telop_map = {
        "s1": {
            "caption_in_offset_sec": 2.0, "caption_out_offset_sec": 3.0,
            "telop_style_hint": {"color": "yellow"},
            "captions": [
                {"caption_in_offset_sec": 2.0, "caption_out_offset_sec": 3.0,
                 "telop_style_hint": {"color": "yellow"}},
                {"caption_in_offset_sec": 12.0, "caption_out_offset_sec": 13.0,
                 "telop_style_hint": {"color": "white"}},
            ],
        },
    }
    shot_ids = ["s1"]
    split_map = [[0, 1]]  # s1 → [new s1 (10s), new s2 (4.8s)]
    new_shot_ids = ["s1", "s2"]
    new_durations = [10.0, 4.8]
    result = director._remap_telops_by_split(telop_map, shot_ids, split_map, new_shot_ids,
                                              new_durations=new_durations)
    # 先頭断片には caption1（start=2s @ fragment0）だけ
    assert "s1" in result
    assert len(result["s1"]["captions"]) == 1
    assert result["s1"]["captions"][0]["caption_in_offset_sec"] == pytest.approx(2.0)
    # 後半断片には caption2（start=12s @ fragment1 の 2s 位置）
    assert "s2" in result
    assert len(result["s2"]["captions"]) == 1
    assert result["s2"]["captions"][0]["caption_in_offset_sec"] == pytest.approx(2.0)  # 12 - 10 = 2s
    assert result["s2"]["captions"][0]["telop_style_hint"]["color"] == "white"


def test_subtitles_multi_caption_hook_uses_correct_default_font_per_style():
    """R4 codex-review P2 修正: 複数 caption の hook shot で、2枚目以降は base フォントを既定にする。"""
    shots = [
        {"id": "s1", "duration_sec": 6.0, "caption_jp": "hook1",
         "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 2.0,
         "captions": [
             {"text": "hook1", "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 2.0},
             {"text": "sub", "caption_in_offset_sec": 3.0, "caption_out_offset_sec": 5.0},
         ]},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots, hook_shot_id="s1")
    assert len(pieces) == 2
    # 1枚目: big
    assert pieces[0]["style"] == "big"
    # 2枚目: base（既定フォントも base=76、big=92 ではない）
    assert pieces[1]["style"] == "base"
    # 2枚目に font_px_override が付くとしたら base 76 からの逸脱で、hint 無しなら付かないはず
    # （旧実装は big=92 から wrap して override が付いてしまっていた）
    assert "font_px_override" not in pieces[1]


def test_subtitles_backward_compat_single_caption():
    """captions[] 無しなら従来どおり caption_jp 単発 piece（後方互換）。"""
    shots = [
        {"id": "s1", "duration_sec": 5.0, "caption_jp": "テロップ",
         "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert len(pieces) == 1
    assert pieces[0]["caption"] == "テロップ"


def test_fidelity_telop_intervals_expand_captions_array():
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "caption_jp": "text",
             "captions": [
                 {"text": "テロップ1", "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 1.5},
                 {"text": "テロップ2", "caption_in_offset_sec": 2.5, "caption_out_offset_sec": 3.5},
             ]},
        ],
    }
    intervals = fidelity._telop_intervals_from_plan(plan)
    assert len(intervals) == 2
    assert intervals[0] == pytest.approx((0.5, 1.5))
    assert intervals[1] == pytest.approx((2.5, 3.5))


def test_fidelity_merge_continuation_preserves_captions_on_fragments():
    """R4 codex-review P1 修正への追随: 分割断片が caption を持つとき、親に集約するときも
    その caption を親 captions[] へ吸収する（無視すると telop_iou の denom がズレる）。"""
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 10.0, "caption_jp": "T0",
             "caption_in_offset_sec": 2.0, "caption_out_offset_sec": 3.0},
            {"id": "s2", "duration_sec": 5.0, "caption_jp": "T1",
             "caption_in_offset_sec": 2.0, "caption_out_offset_sec": 3.0,
             "continuation_of": "s1"},
        ],
    }
    intervals = fidelity._telop_intervals_from_plan(plan)
    assert len(intervals) == 2
    # 親 s1 の caption: [2, 3]
    assert intervals[0] == pytest.approx((2.0, 3.0))
    # 断片 s2 の caption: 親開始からの累積 10 + 2 = 12 → [12, 13]
    assert intervals[1] == pytest.approx((12.0, 13.0))


def test_fidelity_telop_intervals_from_continuation_uses_logical_shots():
    """継続断片は親に統合してから caption を展開する（境界跨ぎ caption offset の意味論を保つ）。"""
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 5.0, "caption_jp": "one",
             "caption_in_offset_sec": 1.0, "caption_out_offset_sec": 3.0},
            {"id": "s2", "duration_sec": 5.0, "caption_jp": "",
             "continuation_of": "s1"},
            {"id": "s3", "duration_sec": 3.0, "caption_jp": "two",
             "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 2.0},
        ],
    }
    intervals = fidelity._telop_intervals_from_plan(plan)
    assert len(intervals) == 2
    # s1 caption: absolute [1.0, 3.0]
    assert intervals[0] == pytest.approx((1.0, 3.0))
    # s3 caption: absolute [10 + 0.5, 10 + 2.0] = [10.5, 12.0]
    assert intervals[1] == pytest.approx((10.5, 12.0))


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------

def _skeleton_to_stub_plan(sk):
    """skeleton をそのまま埋め字した最小 plan を返す（fidelity計算用スタブ）。"""
    shots = []
    for s in sk["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "vp"
        shot["motion_preset"] = shot.get("motion_preset", "static")
        # caption_slots があれば captions[] へ text 追加、なければ caption_jp のみ。
        slots = shot.pop("caption_slots", None)
        if slots:
            shot["captions"] = [
                {"text": "t{}".format(i), **{k: v for k, v in slot.items() if k != "telop_style_hint"}}
                for i, slot in enumerate(slots)
            ]
            shot["caption_jp"] = "t0"
        elif "caption_in_offset_sec" in shot:
            shot["caption_jp"] = "cap"
        else:
            shot["caption_jp"] = ""
        shots.append(shot)
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": shots,
        "sfx_plan": sk.get("sfx_plan") or [],
        "hook_end_shot_id": sk.get("hook_end_shot_id"),
        "cta_start_shot_id": sk.get("cta_start_shot_id"),
        "bgm_mood": "upbeat",
        "meta": {"source": "ai", "shot_ref_ranges": sk.get("shot_ref_ranges")},
    }
