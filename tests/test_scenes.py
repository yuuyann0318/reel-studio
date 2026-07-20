# -*- coding: utf-8 -*-
"""pipeline/scenes.py（シーングループ純関数）の単体テスト。

窓計算(compute_scene_windows)の境界: T_a==T_p / T_a<T_p / T_a>T_p / 末尾tpad /
単独縮退。グルーピング(group_shots_into_scenes)と貪欲分割(split_scene_by_max_request)
の境界（12秒ちょうど / 超過 / 単独超過）を網羅する。
"""
import pytest

from pipeline import scenes


def _shot(sid, dur, scene_id=None):
    s = {"id": sid, "visual_prompt": "p", "motion_preset": "static", "duration_sec": dur, "caption_jp": "c"}
    if scene_id is not None:
        s["scene_id"] = scene_id
    return s


# ---------------------------------------------------------------------------
# group_shots_into_scenes
# ---------------------------------------------------------------------------

def test_group_all_without_scene_id_are_solo_scenes():
    shots = [_shot("s1", 2), _shot("s2", 2), _shot("s3", 2)]
    result = scenes.group_shots_into_scenes(shots)
    assert [g["scene_key"] for g in result] == ["s1", "s2", "s3"]
    assert all(len(g["shots"]) == 1 for g in result)


def test_group_consecutive_same_scene_id_merged():
    shots = [_shot("s1", 2, "sc1"), _shot("s2", 2, "sc1"), _shot("s3", 2, "sc2")]
    result = scenes.group_shots_into_scenes(shots)
    assert [g["scene_key"] for g in result] == ["sc1", "sc2"]
    assert [s["id"] for s in result[0]["shots"]] == ["s1", "s2"]
    assert [s["id"] for s in result[1]["shots"]] == ["s3"]


def test_group_mixed_scene_and_solo():
    shots = [_shot("s1", 2, "sc1"), _shot("s2", 2, "sc1"), _shot("s3", 2), _shot("s4", 2, "sc2")]
    result = scenes.group_shots_into_scenes(shots)
    assert [g["scene_key"] for g in result] == ["sc1", "s3", "sc2"]
    assert [len(g["shots"]) for g in result] == [2, 1, 1]


def test_group_empty_scene_id_treated_as_solo():
    shots = [_shot("s1", 2, "  "), _shot("s2", 2, "")]
    result = scenes.group_shots_into_scenes(shots)
    assert [g["scene_key"] for g in result] == ["s1", "s2"]


# ---------------------------------------------------------------------------
# split_scene_by_max_request
# ---------------------------------------------------------------------------

def test_split_under_max_no_split_keeps_key():
    scene = {"scene_key": "sc1", "shots": [_shot("s1", 5, "sc1"), _shot("s2", 5, "sc1")]}
    result = scenes.split_scene_by_max_request(scene, max_sec=12.0)
    assert len(result) == 1
    assert result[0]["scene_key"] == "sc1"


def test_split_exactly_12_does_not_split():
    scene = {"scene_key": "sc1", "shots": [_shot("s1", 6, "sc1"), _shot("s2", 6, "sc1")]}
    result = scenes.split_scene_by_max_request(scene, max_sec=12.0)
    assert len(result) == 1
    assert result[0]["scene_key"] == "sc1"


def test_split_over_max_greedy_with_suffixes():
    # 5+5+5=15 > 12 → [s1,s2](10)=sc1_a, [s3](5)=sc1_b
    scene = {"scene_key": "sc1", "shots": [_shot("s1", 5, "sc1"), _shot("s2", 5, "sc1"), _shot("s3", 5, "sc1")]}
    result = scenes.split_scene_by_max_request(scene, max_sec=12.0)
    assert [g["scene_key"] for g in result] == ["sc1_a", "sc1_b"]
    assert [s["id"] for s in result[0]["shots"]] == ["s1", "s2"]
    assert [s["id"] for s in result[1]["shots"]] == ["s3"]


def test_split_single_shot_over_max_stays_one_bucket():
    scene = {"scene_key": "sc1", "shots": [_shot("s1", 20, "sc1")]}
    result = scenes.split_scene_by_max_request(scene, max_sec=12.0)
    assert len(result) == 1
    assert result[0]["scene_key"] == "sc1"
    assert [s["id"] for s in result[0]["shots"]] == ["s1"]


def test_split_three_buckets_suffixes():
    # 5秒×5本=25秒, max12 → [s1,s2](10)|[s3,s4](10)|[s5](5) の3バケット → a,b,c
    shots = [_shot("s{}".format(i), 5, "sc1") for i in range(1, 6)]
    result = scenes.split_scene_by_max_request({"scene_key": "sc1", "shots": shots}, max_sec=12.0)
    assert [g["scene_key"] for g in result] == ["sc1_a", "sc1_b", "sc1_c"]
    assert [[s["id"] for s in g["shots"]] for g in result] == [["s1", "s2"], ["s3", "s4"], ["s5"]]


# ---------------------------------------------------------------------------
# compute_scene_windows
# ---------------------------------------------------------------------------

def test_windows_ta_equals_tp_no_pad():
    # 計画 [2,3], T_p=5, T_a=5: 各ショットは計画どおり・パディング不要
    w = scenes.compute_scene_windows([2.0, 3.0], 5.0)
    assert w[0]["trim_start"] == pytest.approx(0.0)
    assert w[0]["content_duration"] == pytest.approx(2.0)
    assert w[0]["pad_to"] == pytest.approx(2.0)
    assert w[1]["trim_start"] == pytest.approx(2.0)
    assert w[1]["content_duration"] == pytest.approx(3.0)
    assert w[1]["pad_to"] == pytest.approx(3.0)


def test_windows_ta_less_than_tp_compresses_offsets_and_pads_tail():
    # 計画 [3,3], T_p=6, T_a=4.5 → scale=0.75。off=[0,3]→start=[0,2.25]
    w = scenes.compute_scene_windows([3.0, 3.0], 4.5)
    assert w[0]["trim_start"] == pytest.approx(0.0)
    assert w[0]["content_duration"] == pytest.approx(3.0)  # 0..3 は T_a=4.5 内
    assert w[0]["pad_to"] == pytest.approx(3.0)
    assert w[1]["trim_start"] == pytest.approx(2.25)
    # content = min(2.25+3, 4.5) - 2.25 = 2.25 < pad_to=3 → 末尾はtpadで埋める
    assert w[1]["content_duration"] == pytest.approx(2.25)
    assert w[1]["pad_to"] == pytest.approx(3.0)
    assert w[1]["content_duration"] < w[1]["pad_to"]


def test_windows_ta_greater_than_tp_no_compression_extra_unused():
    # T_a>T_p: 圧縮せず各ショットは計画どおり。余剰footageは使わない（ループ再利用しない）
    w = scenes.compute_scene_windows([2.0, 2.0], 10.0)
    assert w[0]["trim_start"] == pytest.approx(0.0)
    assert w[0]["content_duration"] == pytest.approx(2.0)
    assert w[1]["trim_start"] == pytest.approx(2.0)
    assert w[1]["content_duration"] == pytest.approx(2.0)
    assert w[1]["pad_to"] == pytest.approx(2.0)


def test_windows_single_shot_degenerate_equals_legacy():
    # 単独シーン・T_a==T_p: trim_start=0, content=pad=dur（従来の1ショット切り出しと等価）
    w = scenes.compute_scene_windows([5.0], 5.0)
    assert len(w) == 1
    assert w[0]["trim_start"] == pytest.approx(0.0)
    assert w[0]["content_duration"] == pytest.approx(5.0)
    assert w[0]["pad_to"] == pytest.approx(5.0)


def test_windows_single_shot_short_master_pads():
    # 単独・T_a<dur: content=T_a, pad_to=dur（不足分はtpad）
    w = scenes.compute_scene_windows([5.0], 3.0)
    assert w[0]["trim_start"] == pytest.approx(0.0)
    assert w[0]["content_duration"] == pytest.approx(3.0)
    assert w[0]["pad_to"] == pytest.approx(5.0)


def test_windows_actual_zero_falls_back_to_planned_total():
    # ffprobe失敗(0/None)相当 → 計画合計へfallbackし圧縮しない
    w = scenes.compute_scene_windows([2.0, 2.0], 0.0)
    assert w[1]["trim_start"] == pytest.approx(2.0)
    assert w[1]["content_duration"] == pytest.approx(2.0)
