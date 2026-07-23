# -*- coding: utf-8 -*-
"""build_shot_skeleton の max_shot_sec による長尺 shot 分割の単体テスト。

TTP v2 P5 申し送り: Higgsfield 480p の credits≒秒 の実測から、
config.higgsfield.max_credits_per_shot を上限として長尺shotを等分割する。
分割時は caption 写像・sfx 写像・hook_end/cta_start の shot_id を維持する。
"""
import json
import os

import pytest

from pipeline import director


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_no_split_when_max_shot_sec_none(real_spec):
    """max_shot_sec=None（既定）は従来挙動を保つ（後方互換）。"""
    sk_default = director.build_shot_skeleton(real_spec, 30.0)
    sk_none = director.build_shot_skeleton(real_spec, 30.0, max_shot_sec=None)
    assert len(sk_default["shots"]) == len(sk_none["shots"])
    for a, b in zip(sk_default["shots"], sk_none["shots"]):
        assert a["id"] == b["id"]
        assert abs(a["duration_sec"] - b["duration_sec"]) < 1e-6


def test_split_when_shot_exceeds_max_shot_sec(real_spec):
    """target=60秒（各shot≒12秒相当）に max_shot_sec=10 を与えると shot 数が増える。"""
    sk_unbounded = director.build_shot_skeleton(real_spec, 60.0)
    sk_capped = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    assert len(sk_capped["shots"]) > len(sk_unbounded["shots"])
    # 各 shot は 10 秒以下
    for s in sk_capped["shots"]:
        assert s["duration_sec"] <= 10.0 + 1e-3


def test_split_preserves_total_duration(real_spec):
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    total = sum(s["duration_sec"] for s in sk["shots"])
    assert abs(total - 60.0) <= 0.5  # 小数丸めの許容


def test_split_preserves_sfx_shot_id_existence(real_spec):
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    valid_ids = {s["id"] for s in sk["shots"]}
    for ev in sk["sfx_plan"] or []:
        anc = ev.get("t_anchor") or {}
        assert anc.get("shot_id") in valid_ids
        # offset は対応shotの尺内
        shot = next(s for s in sk["shots"] if s["id"] == anc["shot_id"])
        assert -1e-6 <= float(anc["offset_sec"]) <= shot["duration_sec"] + 1e-6


def test_split_preserves_hook_and_cta_shot_ids(real_spec):
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    valid_ids = {s["id"] for s in sk["shots"]}
    if sk["hook_end_shot_id"] is not None:
        assert sk["hook_end_shot_id"] in valid_ids
    if sk["cta_start_shot_id"] is not None:
        assert sk["cta_start_shot_id"] in valid_ids


def test_split_preserves_caption_offsets_within_new_shot(real_spec):
    sk = director.build_shot_skeleton(real_spec, 60.0, max_shot_sec=10.0)
    for s in sk["shots"]:
        if "caption_in_offset_sec" in s:
            assert 0.0 <= s["caption_in_offset_sec"] <= s["duration_sec"] + 1e-3
            assert s["caption_in_offset_sec"] <= s["caption_out_offset_sec"] + 1e-6
            assert s["caption_out_offset_sec"] <= s["duration_sec"] + 1e-3


def test_run_director_passes_max_shot_sec_from_config(real_spec, monkeypatch):
    """config.higgsfield.max_credits_per_shot が build_shot_skeleton に渡ることを確認。"""
    captured = {}

    real_build = director.build_shot_skeleton

    def _spy(spec, target, max_shot_sec=None, min_shot_sec=None, **kwargs):
        captured["max_shot_sec"] = max_shot_sec
        captured["min_shot_sec"] = min_shot_sec
        return real_build(spec, target, max_shot_sec=max_shot_sec, min_shot_sec=min_shot_sec, **kwargs)

    monkeypatch.setattr(director, "build_shot_skeleton", _spy)

    # LLM 呼び出しを短絡: _attempt_plan がすぐに骨と同一の plan を返すよう差し替える。
    def _fake_attempt(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                      trace=None, skeleton=None, reference=None):
        # スケルトンに埋め字だけした最小plan を返す（validate_plan は通らないが、
        # ここでは build_shot_skeleton が想定引数で呼ばれることの確認だけが目的）。
        if trace is not None:
            trace["last_model_used"] = "test-model"
        if not skeleton:
            return None
        shots = []
        for s in skeleton["shots"]:
            shot = dict(s)
            shot["visual_prompt"] = "placeholder"
            shot["motion_preset"] = "static"
            shot["caption_jp"] = "テスト"
            shots.append(shot)
        return {
            "version": 2, "meta": {"source": "ai"},
            "concept": "c", "hook": "h", "narration_script": "n",
            "shots": shots, "bgm_mood": "upbeat",
            "sfx_plan": skeleton.get("sfx_plan") or [],
            "hook_end_shot_id": skeleton.get("hook_end_shot_id"),
            "cta_start_shot_id": skeleton.get("cta_start_shot_id"),
        }

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    # call_claude_json は None でないことを確認する分岐なので、任意の callable を入れる
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    director.run_director(
        "theme", config={"higgsfield": {"max_credits_per_shot": 10}},
        target_duration_sec=30.0, reference=real_spec, quality="single",
    )
    assert captured.get("max_shot_sec") == 10.0
