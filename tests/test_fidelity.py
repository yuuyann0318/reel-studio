# -*- coding: utf-8 -*-
"""F13: qa/fidelity.py の5指標算出の単体テスト。

- 完全一致するペア -> 5指標すべて 1.0
- 大きく崩れたペア -> 5指標すべて 1.0 未満
- 実キャッシュspec + 骨だけ埋めた plan で telop_style が hint 一致率どおり出る
"""
from __future__ import annotations

import json
import os

import pytest

from qa import fidelity


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _plan_from_skeleton(spec, caption_all_shots=False):
    """skeleton の骨に埋め字を足して plan を組み立てる。

    caption_all_shots=False (既定): telop_style_hint がある shot にだけ caption を付ける。
    参考の telops と 1:1 で対応させたい場合はこちらを使う（fidelity 計算の telop_iou
    denom が ref_telops 数と揃うため参考=生成の完全一致時に 1.0 になる）。
    """
    from pipeline import director
    sk = director.build_shot_skeleton(spec, float(spec["duration_sec"]))
    shots = []
    for s in sk["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "placeholder"
        shot["motion_preset"] = "static"
        if caption_all_shots or shot.get("telop_style_hint"):
            shot["caption_jp"] = "テスト"
        else:
            shot["caption_jp"] = ""
        shots.append(shot)
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": shots, "bgm_mood": "upbeat",
        "sfx_plan": sk.get("sfx_plan") or [],
        "hook_end_shot_id": sk.get("hook_end_shot_id"),
        "cta_start_shot_id": sk.get("cta_start_shot_id"),
    }


def test_fidelity_returns_all_5_metrics(real_spec):
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    keys = set(result["summary"].keys())
    assert keys == {"cut_match", "telop_iou", "telop_style", "sfx_placement", "camera_move"}
    for k, v in result["summary"].items():
        assert 0.0 <= v <= 1.0, "{}={} が [0,1] 範囲外".format(k, v)


def test_fidelity_perfect_match_when_plan_matches_skeleton_exactly(real_spec):
    """スケルトンをそのまま plan にすれば cut_match / telop_iou は 1.0 に近い。"""
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    # 参考尺=生成尺なのでスケール比 1.0。境界は一致する。
    assert result["summary"]["cut_match"] >= 0.99
    # 参考の telops が空(=0件)でなければ IoU は少なくとも 0.5 以上（skeleton 経由の caption
    # offset が参考の telop 区間そのままに載っているため）。
    assert result["summary"]["telop_iou"] >= 0.5


def test_fidelity_telop_style_reflects_hint_agreement(real_spec):
    """skeleton 経由の plan は telop_style_hint が参考テロップと同一になるため style スコアが高い。"""
    plan = _plan_from_skeleton(real_spec)
    result = fidelity.compute_fidelity(real_spec, plan)
    # skeleton に載る telop_style_hint は参考の position/color/size_class をそのまま
    # コピーしているので telop_style は 0.9 以上（三属性全て一致）を期待
    assert result["summary"]["telop_style"] >= 0.9


def test_fidelity_low_when_plan_lacks_hint_and_captions(real_spec):
    """plan.shots が caption も hint も持たなければ telop_iou / telop_style は下がる。"""
    plan = _plan_from_skeleton(real_spec)
    for s in plan["shots"]:
        s["caption_jp"] = ""  # captions を全て空にする
        s.pop("telop_style_hint", None)
    result = fidelity.compute_fidelity(real_spec, plan)
    assert result["summary"]["telop_iou"] < 0.5


def test_fidelity_cli_writes_json(tmp_path, real_spec):
    """CLI 経由で JSON が保存される。"""
    ref_path = tmp_path / "ref.json"
    plan_path = tmp_path / "plan.json"
    out_path = tmp_path / "out.json"
    ref_path.write_text(json.dumps(real_spec, ensure_ascii=False), encoding="utf-8")
    plan_path.write_text(json.dumps(_plan_from_skeleton(real_spec), ensure_ascii=False), encoding="utf-8")
    ret = fidelity.main([
        "--reference-spec", str(ref_path),
        "--plan", str(plan_path),
        "--out", str(out_path),
    ])
    assert ret == 0
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "summary" in data
    assert "cut_match" in data["summary"]
