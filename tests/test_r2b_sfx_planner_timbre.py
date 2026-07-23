# -*- coding: utf-8 -*-
"""R2b F6: sfx_planner.resolve_sfx_events が timbre 情報で選定を切り替えるテスト。"""
from __future__ import annotations

import os

import pytest

from pipeline import sfx_planner
from pipeline import edit_profile


class FakeCache:
    """SfxTimbreCache 互換。file名 → MFCC 辞書で決定論に返す。"""

    def __init__(self, mfcc_by_file):
        self._map = mfcc_by_file

    def mfcc_for(self, sfx_path):
        return self._map.get(os.path.basename(sfx_path), [])


class TestFindRefEventFor:
    def test_selects_closest_same_family(self):
        events = [
            {"t": 1.0, "kind": "impact", "timbre_mfcc": [1.0, 2.0]},
            {"t": 5.0, "kind": "impact", "timbre_mfcc": [3.0, 4.0]},
            {"t": 1.2, "kind": "transition", "timbre_mfcc": [9.0]},
        ]
        # plan_at_sec=2.0（gen尺=10, ref尺=10）で family=impact に一致する最寄りは t=1.0
        ev = sfx_planner._find_ref_event_for("impact", 2.0, events, 10.0, 10.0)
        assert ev is not None
        assert ev["timbre_mfcc"] == [1.0, 2.0]

    def test_returns_none_when_no_family_match(self):
        events = [{"t": 1.0, "kind": "pop"}]
        ev = sfx_planner._find_ref_event_for("impact", 1.0, events, 10.0, 10.0)
        assert ev is None

    def test_respects_ratio_scaling(self):
        # ref=20s, gen=10s, plan_at=4s → ref_t=8s
        events = [
            {"t": 2.0, "kind": "impact", "timbre_mfcc": [1.0]},
            {"t": 8.5, "kind": "impact", "timbre_mfcc": [2.0]},
        ]
        ev = sfx_planner._find_ref_event_for("impact", 4.0, events, 20.0, 10.0)
        assert ev is not None
        assert ev["timbre_mfcc"] == [2.0]  # 8.5 の方が 8 に近い


class TestResolveSfxEventsWithTimbre:
    def _minimal_plan(self):
        return {
            "shots": [
                {"id": "s1", "duration_sec": 4.0},
                {"id": "s2", "duration_sec": 4.0},
            ],
            "sfx_plan": [
                {"t_anchor": {"type": "shot_start", "shot_id": "s2", "offset_sec": 0.5},
                 "family": "impact"},
            ],
        }

    def test_falls_back_to_deterministic_pick_when_no_reference_events(self, monkeypatch):
        # reference_sfx_events を渡さない → 既存の pick_cut_sfx が使われる
        plan = self._minimal_plan()
        manifest = [
            {"file": "impact_A.wav", "family": "impact"},
            {"file": "impact_B.wav", "family": "impact"},
        ]

        def fake_pick(**kwargs):
            return kwargs["manifest"][0]

        def fake_resolve(name):
            return "/fake/{}".format(name)

        monkeypatch.setattr(edit_profile, "pick_cut_sfx", fake_pick)
        monkeypatch.setattr(edit_profile, "resolve_sfx_file_path", fake_resolve)
        specs = sfx_planner.resolve_sfx_events(plan, [4.0, 4.0], manifest=manifest)
        assert len(specs) == 1
        assert specs[0]["path"] == "/fake/impact_A.wav"

    def test_uses_timbre_matcher_when_reference_events_provided(self, monkeypatch):
        plan = self._minimal_plan()
        # 参考 SFX と近い MFCC を持つファイルを B にする → timbre 経路は B を選ぶ
        manifest = [
            {"file": "impact_A.wav", "family": "impact"},
            {"file": "impact_B.wav", "family": "impact"},
        ]
        ref_events = [
            {"t": 4.5, "kind": "impact", "timbre_mfcc": [0.0, 1.0]},
        ]
        cache = FakeCache({
            "impact_A.wav": [1.0, 0.0],  # target と直交
            "impact_B.wav": [0.0, 1.0],  # target と一致
        })

        def fake_resolve(name):
            return "/fake/{}".format(name)

        # 決定論経路が呼ばれないことも確認するため、fake_pick は例外を投げる
        def fake_pick(**kwargs):
            raise RuntimeError("should not be called when timbre matches")

        monkeypatch.setattr(edit_profile, "pick_cut_sfx", fake_pick)
        monkeypatch.setattr(edit_profile, "resolve_sfx_file_path", fake_resolve)

        specs = sfx_planner.resolve_sfx_events(
            plan, [4.0, 4.0], manifest=manifest,
            reference_sfx_events=ref_events, reference_duration_sec=8.0,
            sfx_timbre_cache=cache,
        )
        assert len(specs) == 1
        assert specs[0]["path"] == "/fake/impact_B.wav"

    def test_timbre_no_family_match_falls_back(self, monkeypatch):
        plan = self._minimal_plan()
        manifest = [{"file": "impact_A.wav", "family": "impact"}]
        ref_events = [{"t": 5.0, "kind": "pop", "timbre_mfcc": [1.0]}]  # 別family
        cache = FakeCache({"impact_A.wav": [1.0]})

        def fake_pick(**kwargs):
            return kwargs["manifest"][0]

        def fake_resolve(name):
            return "/fake/{}".format(name)

        monkeypatch.setattr(edit_profile, "pick_cut_sfx", fake_pick)
        monkeypatch.setattr(edit_profile, "resolve_sfx_file_path", fake_resolve)

        specs = sfx_planner.resolve_sfx_events(
            plan, [4.0, 4.0], manifest=manifest,
            reference_sfx_events=ref_events, reference_duration_sec=8.0,
            sfx_timbre_cache=cache,
        )
        assert len(specs) == 1
        # family一致 refがないので決定論経路（fake_pick）が使われた
        assert specs[0]["path"] == "/fake/impact_A.wav"
