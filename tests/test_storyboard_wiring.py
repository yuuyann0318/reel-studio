# -*- coding: utf-8 -*-
"""統合配線テスト（3秒 storyboard → visual_prompt / has_person → persona_anchor /
narration_mode の spec 優先）。

背景（両担当の申し送りギャップ）:
  1. spec.storyboard（3秒毎の密記述・on_screen_text・person_desc）を director が
     visual_prompt 生成で未消費だった。
  2. persona_anchor の発火条件 reference_visual.has_person を director が立てていなかった。
  3. spec.narration_mode（reference_v2 が生成）より _infer フォールバックが優先だった。

ここでは skeleton 側（機械配線）を単体検証し、mock backend で has_person→persona_anchor
の実発火をメタ記録で確認する。LLM 生成そのものは検証せず、プロンプトテンプレの文言と
skeleton の骨情報だけを機械確認する。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from pipeline import director, reference_v2
from pipeline.visual import mock_backend


# ---------------------------------------------------------------------------
# storyboard 付きの最小 spec を組み立てる（3秒粒度・人物あり・on_screen_text あり）
# ---------------------------------------------------------------------------
def _spec_with_storyboard():
    return {
        "version": 2,
        "url": "https://example.test/reel",
        "duration_sec": 12.0,
        "transcript": "",
        "cuts": [{"t": 3.0, "confidence": 0.9}, {"t": 6.0, "confidence": 0.9},
                 {"t": 9.0, "confidence": 0.9}],
        "shots_ref": [
            {"start": 0.0, "end": 3.0, "visual_desc_en": "person applying serum", "motion": "static"},
            {"start": 3.0, "end": 6.0, "visual_desc_en": "closeup cheek", "motion": "zoom_in"},
            {"start": 6.0, "end": 9.0, "visual_desc_en": "closeup forehead", "motion": "static"},
            {"start": 9.0, "end": 12.0, "visual_desc_en": "after result", "motion": "static"},
        ],
        "telops": [
            {"start": 0.1, "end": 2.9, "text": "10%/100%", "position": "top", "confidence": 0.9},
            {"start": 3.1, "end": 5.9, "text": "35%/100%", "position": "top", "confidence": 0.9},
            {"start": 6.1, "end": 8.9, "text": "75%/100%", "position": "top", "confidence": 0.9},
            {"start": 9.1, "end": 11.9, "text": "〜After〜", "position": "top", "confidence": 0.9},
        ],
        "sfx_events": [],
        "bgm": {"present": True, "mood_guess": "upbeat pop"},
        "storyboard": [
            {"t": 1.5, "description_ja": "黒髪の男性が白い美容液容器を持ち頬に垂らす",
             "on_screen_text": "10%/100%\n※効果効能を保証するものではありません",
             "has_person": True, "person_desc": "30代前後の黒髪の男性",
             "objects": ["美容液容器", "スポイト"]},
            {"t": 4.5, "description_ja": "男性の額のアップ。透明なヘラで塗り広げる",
             "on_screen_text": "35%/100%\n※個人差があります",
             "has_person": True, "person_desc": "30代前後の黒髪の男性",
             "objects": ["透明なヘラ状の道具"]},
            {"t": 7.5, "description_ja": "男性の横顔のアップ。頬に美容液を垂らす",
             "on_screen_text": "75%/100%", "has_person": True,
             "person_desc": "30代前後の黒髪の男性", "objects": ["美容液容器"]},
            {"t": 10.5, "description_ja": "男性が上を向き指で顎に触れる。肌が艶やか",
             "on_screen_text": "〜After〜", "has_person": True,
             "person_desc": "30代前後の黒髪の男性", "objects": ["指"]},
        ],
    }


# ---------------------------------------------------------------------------
# (1) storyboard → shot 写像（純関数）: 含有優先 + 最寄りフォールバック被覆
# ---------------------------------------------------------------------------
def test_map_storyboard_containment_and_nearest_fallback():
    storyboard = [
        {"t": 1.0, "description_ja": "A", "on_screen_text": "x", "has_person": True,
         "person_desc": "p", "objects": ["a1"]},
        {"t": 5.0, "description_ja": "B", "on_screen_text": "y", "has_person": False,
         "person_desc": "", "objects": ["b1"]},
    ]
    boundaries = [0.0, 2.0, 4.0, 6.0]
    shot_ids = ["s1", "s2", "s3"]
    m = director._map_storyboard_to_shots(storyboard, boundaries, shot_ids)
    # s1(0-2) は t=1.0 を含有 → A
    assert m["s1"]["scene_desc_ja"] == "A"
    assert m["s1"]["has_person"] is True
    # s2(2-4) は含有 sb 無し → 最寄り(t=1.0 or 5.0、中点3.0で等距離→先頭t=1.0)
    assert "scene_desc_ja" in m["s2"]
    # s3(4-6) は t=5.0 を含有 → B
    assert m["s3"]["scene_desc_ja"] == "B"


def test_map_storyboard_drops_disclaimer_lines():
    storyboard = [{"t": 1.0, "description_ja": "d", "has_person": False,
                   "on_screen_text": "10%/100%\n※効果効能を保証するものではありません\n※個人差があります",
                   "objects": []}]
    m = director._map_storyboard_to_shots(storyboard, [0.0, 2.0], ["s1"])
    ost = m["s1"]["on_screen_text"]
    assert "10%/100%" in ost
    assert "※" not in ost  # 免責/注記行は落ちる


def test_map_storyboard_empty_returns_empty():
    assert director._map_storyboard_to_shots([], [0.0, 2.0], ["s1"]) == {}
    assert director._map_storyboard_to_shots(None, [0.0, 2.0], ["s1"]) == {}


# ---------------------------------------------------------------------------
# (2) skeleton の各 shot に storyboard 由来フィールドが載る
# ---------------------------------------------------------------------------
def test_skeleton_carries_storyboard_scene_desc():
    spec = _spec_with_storyboard()
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    rv_shots = [s for s in sk["shots"] if "reference_visual" in s]
    assert rv_shots
    # 全 shot が scene_desc_ja と has_person を持つ（storyboard は全区間被覆する）
    for s in sk["shots"]:
        rv = s.get("reference_visual") or {}
        assert rv.get("scene_desc_ja"), "shot {} に scene_desc_ja が無い".format(s["id"])
        assert rv.get("has_person") is True, "shot {} に has_person が無い".format(s["id"])
    # objects / person_desc も少なくとも1 shot に載る
    assert any((s.get("reference_visual") or {}).get("objects") for s in sk["shots"])
    assert any((s.get("reference_visual") or {}).get("person_desc") for s in sk["shots"])


def test_skeleton_storyboard_survives_credit_split():
    spec = _spec_with_storyboard()
    sk = director.build_shot_skeleton(spec, 24.0, max_shot_sec=3.0, min_shot_sec=0.5)
    # 分割断片にも scene_desc_ja / has_person が残る
    rv_shots = [s for s in sk["shots"] if (s.get("reference_visual") or {}).get("scene_desc_ja")]
    assert rv_shots
    assert all((s.get("reference_visual") or {}).get("has_person") is True
               for s in sk["shots"] if "reference_visual" in s)


# ---------------------------------------------------------------------------
# (3) has_person → persona_anchor が mock backend で実発火（メタ記録）
# ---------------------------------------------------------------------------
def _fake_ok_run(monkeypatch):
    class _P:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())


def test_storyboard_has_person_triggers_persona_anchor(monkeypatch, tmp_path):
    _fake_ok_run(monkeypatch)
    spec = _spec_with_storyboard()
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    backend = mock_backend.MockBackend({})
    metas = []
    for i, s in enumerate(sk["shots"]):
        # skeleton の shot は visual_prompt を持たないので mock 用に最小フィールドを補う
        shot = dict(s)
        shot.setdefault("visual_prompt", "abstract background")
        shot.setdefault("motion_preset", "static")
        metas.append(backend.generate(shot, str(tmp_path / "shot{}.mp4".format(i))))
    anchors = [m["persona_anchor"] for m in metas]
    # 全 shot が人物 → 1本目 captured、以降 applied。none は無い。
    assert anchors[0] == "captured"
    assert all(a == "applied" for a in anchors[1:])
    assert "none" not in anchors


# ---------------------------------------------------------------------------
# (4) narration_mode: reference_v2 生成ロジック
# ---------------------------------------------------------------------------
def test_compute_narration_mode_present():
    spec = {"transcript": "こんにちは今日は", "telops": []}
    assert reference_v2.compute_reference_narration_mode(spec, asr_ok=True) == "present"


def test_compute_narration_mode_absent_asr_success_no_speech():
    spec = {"transcript": "", "telops": []}
    # ASR は成功したがセリフ実在せず → absent
    assert reference_v2.compute_reference_narration_mode(spec, asr_ok=True) == "absent"


def test_compute_narration_mode_absent_asr_failed_rich_telops():
    spec = {"transcript": "",
            "telops": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    assert reference_v2.compute_reference_narration_mode(spec, asr_ok=False) == "absent"


def test_compute_narration_mode_unknown_asr_failed_few_telops():
    spec = {"transcript": "", "telops": [{"text": "a"}]}
    assert reference_v2.compute_reference_narration_mode(spec, asr_ok=False) == "unknown"


# ---------------------------------------------------------------------------
# (5) director が spec.narration_mode を _infer より優先する
# ---------------------------------------------------------------------------
def test_director_prefers_spec_narration_mode_over_infer():
    spec = _spec_with_storyboard()
    # _infer は telops>=3 かつ bgm present → absent を返すはず。ここで spec 側を
    # "present" に上書きし、優先されることを確認する（フォールバックとの分離検証）。
    spec["narration_mode"] = "present"
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    assert sk["narration_mode"] == "present"


def test_director_falls_back_to_infer_when_spec_missing():
    spec = _spec_with_storyboard()
    spec.pop("narration_mode", None)
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    # transcript 空 + telops>=3 + bgm present → _infer は absent
    assert sk["narration_mode"] == "absent"


def test_director_ignores_invalid_spec_narration_mode():
    spec = _spec_with_storyboard()
    spec["narration_mode"] = "garbage"
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    assert sk["narration_mode"] in ("present", "absent", "unknown")
    assert sk["narration_mode"] == "absent"  # _infer フォールバック


# ---------------------------------------------------------------------------
# (6) プロンプトテンプレが scene_desc_ja 由来の visual_prompt 指示を含む
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# (7) telop_iou 改善: カットまたぎテロップを最大重複 shot に載せる
# ---------------------------------------------------------------------------
def test_resolve_shot_id_max_overlap_picks_dominant_shot():
    boundaries = [0.0, 2.6, 3.9, 6.3]
    shot_ids = ["s1", "s2", "s3"]
    # テロップ 2.33-4.07 は s1(0.27) < s2(1.3) > s3(0.17) → 最大重複 s2
    assert director._resolve_shot_id_max_overlap(boundaries, shot_ids, 2.33, 4.07) == "s2"
    # 完全に s1 内のテロップは s1
    assert director._resolve_shot_id_max_overlap(boundaries, shot_ids, 0.1, 2.0) == "s1"


def test_resolve_shot_id_max_overlap_no_overlap_falls_back_to_start():
    boundaries = [0.0, 2.0, 4.0]
    shot_ids = ["s1", "s2"]
    # 区間長ゼロ(点)で境界外 → start ベースのフォールバック
    got = director._resolve_shot_id_max_overlap(boundaries, shot_ids, 10.0, 10.0)
    assert got in ("s1", "s2")


def test_spanning_telop_lands_on_max_overlap_shot():
    """参考の 3秒帯を越えて表示され続けるテロップは、開始 shot ではなく最も長く
    重なる shot の caption として載る（telop_iou の過小評価を防ぐ）。"""
    spec = _spec_with_storyboard()
    # 既定の telops は各 shot 内に収まる。カットまたぎテロップを1本足す:
    # 0.1-3.5 は s1(cut 3.0)を少し(0.5s)、s2(3.0-6.0)を多く(0.5s)... ではなく
    # cuts=[3,6,9] → shots ref [0-3,3-6,6-9,9-12]。1.0-4.0 は s1(2.0) vs s2(1.0) → s1。
    # 2.0-5.0 は s1(1.0) vs s2(2.0) → s2。後者で検証する。
    spec["telops"] = [{"start": 2.0, "end": 5.0, "text": "cross", "position": "top", "confidence": 0.9}]
    sk = director.build_shot_skeleton(spec, 12.0, min_shot_sec=0.5)
    # ref_text="cross" が載る shot を特定
    hosts = [s["id"] for s in sk["shots"] if (s.get("ref_caption_text") == "cross"
             or any(c.get("ref_text") == "cross" for c in (s.get("caption_slots") or [])))]
    assert hosts, "cross テロップがどこかの shot に載ること"
    # s2 が最大重複（2.0-5.0 は shot2(3-6)と 2.0s 重なる）。境界マージが無ければ s2。
    # マージで shot 構成が変わる可能性に配慮し「最大重複 shot」を実計算で照合する。
    boundaries = director._merge_boundaries_to_fit_min_shot(
        director._iter_boundaries_from_spec(spec), 12.0, min_shot_sec=0.5)
    shot_ids = ["s{}".format(i + 1) for i in range(len(boundaries) - 1)]
    expected = director._resolve_shot_id_max_overlap(boundaries, shot_ids, 2.0, 5.0)
    assert expected in hosts


def test_ttp_prompt_instructs_scene_desc_translation():
    path = os.path.join(os.path.dirname(director.__file__), "prompts", "reference_ttp_block.txt")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "scene_desc_ja" in text
    assert "英訳" in text or "翻訳" in text
    # 「発明禁止」の趣旨が明記されている
    assert "発明" in text
