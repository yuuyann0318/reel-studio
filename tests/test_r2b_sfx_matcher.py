# -*- coding: utf-8 -*-
"""R2b F6: SE音色マッチング（純関数部）のテスト。"""
from __future__ import annotations

import json
import os

import pytest

from pipeline import sfx_matcher


class TestCosineSim:
    def test_identical_vectors(self):
        assert sfx_matcher._cosine_sim([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert sfx_matcher._cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert sfx_matcher._cosine_sim([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_or_mismatched_lengths_returns_zero(self):
        assert sfx_matcher._cosine_sim([], [1.0]) == 0.0
        assert sfx_matcher._cosine_sim([1.0], [1.0, 2.0]) == 0.0


class TestPickBestByTimbre:
    def test_returns_first_when_no_target_or_cache(self):
        entries = [{"file": "a.wav"}, {"file": "b.wav"}]
        # target_mfcc 空 → 先頭
        p = sfx_matcher.pick_best_by_timbre([], entries)
        assert p["file"] == "a.wav"
        # cache None → 先頭
        p2 = sfx_matcher.pick_best_by_timbre([1.0, 2.0, 3.0], entries, cache=None)
        assert p2["file"] == "a.wav"

    def test_returns_most_similar_with_stub_cache(self):
        entries = [
            {"file": "aa.wav"},
            {"file": "bb.wav"},
            {"file": "cc.wav"},
        ]
        # Cache スタブ: file → 固定 mfcc
        class StubCache:
            def mfcc_for(self, sfx_path):
                return {"aa.wav": [1.0, 0.0, 0.0],
                        "bb.wav": [0.0, 1.0, 0.0],
                        "cc.wav": [1.0, 0.9, 0.0]}[os.path.basename(sfx_path)]

        # target と最も似ているのは cc.wav
        p = sfx_matcher.pick_best_by_timbre([1.0, 1.0, 0.0], entries, cache=StubCache())
        assert p["file"] == "cc.wav"

    def test_avoids_file_when_multiple_candidates(self):
        entries = [{"file": "x.wav"}, {"file": "y.wav"}]

        class StubCache:
            def mfcc_for(self, sfx_path):
                return [1.0, 0.0]

        # x.wav が最も似ているが avoid_file で除外
        p = sfx_matcher.pick_best_by_timbre([1.0, 0.0], entries, cache=StubCache(), avoid_file="x.wav")
        assert p["file"] == "y.wav"

    def test_empty_family_returns_none(self):
        p = sfx_matcher.pick_best_by_timbre([1.0], [], cache=None)
        assert p is None


class TestTimbreCachePersistence:
    def test_load_from_json_and_save_back(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        # 事前作成した JSON を読ませ、mfcc_for がそれを返すことを確認
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"foo.wav": {"mfcc": [1.0, 2.0, 3.0]}}, f)
        cache = sfx_matcher.SfxTimbreCache(cache_path)
        assert cache.mfcc_for("/anywhere/foo.wav") == [1.0, 2.0, 3.0]

    def test_returns_empty_when_missing_and_librosa_absent_or_file_missing(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        cache = sfx_matcher.SfxTimbreCache(cache_path)
        # 存在しないファイルは [] （librosa 側が None を返しても sfx_matcher が救う）
        assert cache.mfcc_for("/nonexistent/xyz.wav") == []


class TestComputeReferenceEventMfccs:
    def test_missing_audio_returns_events_with_empty_mfcc(self, tmp_path):
        events = [{"t": 1.0, "kind": "transition"},
                  {"t": 2.0, "kind": "impact"}]
        out = sfx_matcher.compute_reference_event_mfccs(str(tmp_path / "no.wav"), events)
        assert len(out) == 2
        assert out[0]["timbre_mfcc"] == []
        assert out[0]["kind"] == "transition"  # 元フィールド保持
