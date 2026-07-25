# -*- coding: utf-8 -*-
"""pipeline/reference_v2.py のテスト。実ネットワーク/実yt-dlp/実ffmpeg/実claudeは一切呼ばない
(全てDI or 純関数のパーサ経由)。"""
import json
import os

import pytest

from pipeline import reference as ref_v1
from pipeline import reference_v2 as v2


# ---------------------------------------------------------------------------
# (a) showinfo stderr パーサ
# ---------------------------------------------------------------------------

def test_parse_showinfo_stderr_extracts_pts_times():
    stderr = (
        "[Parsed_showinfo_1 @ 0x123] n:   0 pts:      0 pts_time:0.0000 pos:  1234 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 0x123] n:   1 pts:   2048 pts_time:2.500 pos:  5678 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 0x123] n:   2 pts:   4096 pts_time:5.75  pos:  9999 fmt:yuv420p\n"
    )
    cuts = v2.parse_showinfo_stderr(stderr)
    assert cuts == [0.0, 2.5, 5.75]


def test_parse_showinfo_stderr_empty():
    assert v2.parse_showinfo_stderr("") == []
    assert v2.parse_showinfo_stderr("no pts here\n") == []


def test_parse_showinfo_stderr_dedups_tight_duplicates():
    stderr = "pts_time:1.000\npts_time:1.001\npts_time:1.500\n"
    cuts = v2.parse_showinfo_stderr(stderr)
    # 0.02秒未満は重複扱い
    assert cuts == [1.0, 1.5]


# ---------------------------------------------------------------------------
# (b) astats/ametadata パーサ + オンセット
# ---------------------------------------------------------------------------

def test_parse_ametadata_stderr_pairs_time_with_rms():
    stderr = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.astats.Overall.RMS_level=-40.55\n"
        "frame:1    pts:2048    pts_time:0.0464\n"
        "lavfi.astats.Overall.RMS_level=-38.10\n"
        "frame:2    pts:4096    pts_time:0.0928\n"
        "lavfi.astats.Overall.RMS_level=-30.00\n"
    )
    series = v2.parse_ametadata_stderr(stderr)
    assert series == [(0.0, -40.55), (0.0464, -38.10), (0.0928, -30.00)]


def test_parse_ametadata_stderr_handles_inf_and_nan():
    stderr = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.astats.Overall.RMS_level=-inf\n"
        "frame:1    pts:2048    pts_time:0.05\n"
        "lavfi.astats.Overall.RMS_level=nan\n"
        "frame:2    pts:4096    pts_time:0.10\n"
        "lavfi.astats.Overall.RMS_level=-20.00\n"
    )
    series = v2.parse_ametadata_stderr(stderr)
    assert series[0] == (0.0, -120.0)
    assert series[1] == (0.05, -120.0)
    assert series[2] == (0.10, -20.0)


def test_detect_onsets_finds_6db_jump():
    series = [(0.0, -40.0), (0.05, -40.0), (0.10, -30.0), (0.15, -30.0), (0.30, -34.0), (0.35, -20.0)]
    onsets = v2.detect_onsets_from_rms_series(series, jump_db=6.0)
    # 0.10 で -40 → -30 の +10dB, 0.35 で -34 → -20 の +14dB
    # 0.10 と 0.35 の間は 0.25s > 0.15s 不応期
    assert 0.10 in onsets
    assert 0.35 in onsets


def test_detect_onsets_refractory_suppresses_double():
    series = [(0.0, -40.0), (0.05, -30.0), (0.10, -20.0)]
    onsets = v2.detect_onsets_from_rms_series(series, jump_db=6.0)
    # 不応期0.15秒なので 0.05 のみ
    assert onsets == [0.05]


# ---------------------------------------------------------------------------
# (c) オンセット分類 (transition / riser / impact / 発話除外 / shimmer)
# ---------------------------------------------------------------------------

def test_classify_onsets_transition_at_cut():
    cuts = [2.0, 5.0]
    events = v2.classify_onsets(cuts, speech_segments=[], hf_onsets=[], full_onsets=[2.03])
    assert any(e["kind"] == "transition" and e["anchor"] == "cut" for e in events)


def test_classify_onsets_riser_before_cut():
    cuts = [3.0]
    events = v2.classify_onsets(cuts, speech_segments=[], hf_onsets=[], full_onsets=[2.0])
    # 3.0 - 2.0 = 1.0s → riser 帯 [0.5, 1.5]
    kinds = [e["kind"] for e in events]
    assert "riser" in kinds


def test_classify_onsets_speech_excludes_full_band():
    cuts = [10.0]
    speech = [{"start": 1.0, "end": 4.0}]
    # full 帯 2.5s は発話区間内 → 除外
    events = v2.classify_onsets(cuts, speech_segments=speech, hf_onsets=[], full_onsets=[2.5])
    # full 側は棄却されるが、HF 由来ではないので何も残らない
    assert events == []


def test_classify_onsets_hf_survives_speech_as_shimmer():
    events = v2.classify_onsets(
        cuts=[10.0], speech_segments=[{"start": 1.0, "end": 4.0}],
        hf_onsets=[2.5], full_onsets=[],
    )
    kinds = [e["kind"] for e in events]
    assert "shimmer" in kinds


def test_classify_onsets_isolated_full_becomes_impact():
    cuts = [10.0]
    events = v2.classify_onsets(cuts, speech_segments=[], hf_onsets=[], full_onsets=[3.0])
    kinds = [e["kind"] for e in events]
    assert "impact" in kinds


# ---------------------------------------------------------------------------
# (d) validate_reference_spec_v2 正常/異常
# ---------------------------------------------------------------------------

def _minimal_valid_v2():
    return {
        "version": 2,
        "url": "https://x.com/v/1",
        "duration_sec": 20.0,
        "transcript": "テスト",
        "segments": [],
        "beats": [{"role": "hook", "start": 0.0, "end": 2.0, "text": "t", "summary": ""}],
        "rhythm": {"sentence_count": 1, "avg_sentence_len": 5, "max_sentence_len": 5, "tone": "", "endings": []},
        "cuts": [{"t": 2.0, "confidence": 0.9}, {"t": 5.0, "confidence": 0.9}],
        "shots_ref": [{"index": 0, "start": 0.0, "end": 2.0, "visual_desc_en": "", "motion": "static"}],
        "telops": [{"start": 0.5, "end": 2.0, "text": "hi", "position": "top",
                    "style": {"color": "white", "stroke": "", "size_class": ""}, "emphasis_words": []}],
        "sfx_events": [{"t": 2.0, "kind": "transition", "anchor": "cut", "confidence": 0.9}],
        "bgm": {"present": True, "mood_guess": ""},
        "warnings": [],
    }


def test_validate_v2_accepts_minimal_valid_spec():
    ok, errors, spec = v2.validate_reference_spec_v2(_minimal_valid_v2())
    assert ok, errors
    assert spec is not None
    assert spec["version"] == 2


def test_validate_v2_rejects_non_dict():
    ok, errors, spec = v2.validate_reference_spec_v2([1, 2, 3])
    assert not ok
    assert spec is None


def test_validate_v2_rejects_missing_keys():
    bad = _minimal_valid_v2()
    del bad["cuts"]
    ok, errors, _ = v2.validate_reference_spec_v2(bad)
    assert not ok
    assert any("cuts" in e or "必須" in e for e in errors)


def test_validate_v2_rejects_non_monotonic_cuts():
    bad = _minimal_valid_v2()
    bad["cuts"] = [{"t": 5.0}, {"t": 2.0}]
    ok, errors, _ = v2.validate_reference_spec_v2(bad)
    assert not ok
    assert any("単調" in e for e in errors)


def test_validate_v2_rejects_time_out_of_range():
    bad = _minimal_valid_v2()
    bad["cuts"] = [{"t": 99.0}]  # duration 20s
    ok, errors, _ = v2.validate_reference_spec_v2(bad)
    assert not ok


def test_validate_v2_rejects_bad_sfx_kind():
    bad = _minimal_valid_v2()
    bad["sfx_events"] = [{"t": 1.0, "kind": "explosion", "anchor": "cut", "confidence": 0.5}]
    ok, errors, _ = v2.validate_reference_spec_v2(bad)
    assert not ok
    assert any("kind" in e for e in errors)


def test_validate_v2_rejects_wrong_version():
    bad = _minimal_valid_v2()
    bad["version"] = 1
    ok, errors, _ = v2.validate_reference_spec_v2(bad)
    assert not ok


# ---------------------------------------------------------------------------
# (e) フレーム間引きロジック
# ---------------------------------------------------------------------------

def test_select_frame_times_two_per_segment():
    # 3セグメント (0-4, 4-8, 8-12) → 各2枚 = 6枚
    times = v2.select_frame_times_from_cuts([4.0, 8.0], duration_sec=12.0, max_frames=40)
    assert 3 <= len(times) <= 6
    # 全て [0, 12] に収まる
    assert all(0 <= t <= 12 for t in times)


def test_select_frame_times_respects_max_frames():
    # 20カット → 42枚 → 上限40で間引き
    cuts = [float(i) for i in range(1, 21)]
    times = v2.select_frame_times_from_cuts(cuts, duration_sec=25.0, max_frames=40)
    assert len(times) <= 40


def test_select_frame_times_skips_micro_segments():
    # 0.05秒しかないセグメントはスキップされる
    times = v2.select_frame_times_from_cuts([0.05], duration_sec=10.0, max_frames=40)
    # 最初のマイクロ区間は無視、[0.05, 10] からは2枚出る
    assert len(times) == 2


# ---------------------------------------------------------------------------
# (f) キャッシュキーが v1 と非衝突
# ---------------------------------------------------------------------------

def test_cache_key_v2_does_not_collide_with_v1(tmp_path):
    cfg = {"reference": {"cache_dir": str(tmp_path)}}
    url = "https://x.com/foo/bar"
    v1_path = ref_v1._cache_path_for(ref_v1.normalize_url(url), cfg)
    v2_path = v2._cache_path_for_v2(ref_v1.normalize_url(url), cfg)
    assert v1_path != v2_path
    assert v2_path.endswith("_v2.json")
    assert not v1_path.endswith("_v2.json")


def test_cache_hit_returns_cached_spec(tmp_path):
    cfg = {"reference": {"cache_dir": str(tmp_path)}}
    url = "https://x.com/a"
    cache_path = v2._cache_path_for_v2(ref_v1.normalize_url(url), cfg)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    cached = _minimal_valid_v2()
    cached["url"] = url
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cached, f)

    def _boom(*a, **k):
        raise AssertionError("キャッシュヒット時に外部呼び出しが発生してはいけない")

    result = v2.analyze_reference_v2(
        url, cfg=cfg,
        fetch_video=_boom, detect_cuts=_boom, extract_frames=_boom, vision_call=_boom,
        ffmpeg_run_ametadata=_boom, asr_post=_boom, claude_call=_boom, fusion_call=_boom,
    )
    assert result["ok"] is True
    assert result["cached"] is True
    assert result["source"] == "cache"
    assert result["spec"]["url"] == url


# ---------------------------------------------------------------------------
# 統合: analyze_reference_v2 の DI 経路をすべてスタブ化して1周させる
# ---------------------------------------------------------------------------

def test_analyze_reference_v2_end_to_end_with_stubs(tmp_path):
    cfg = {
        "reference": {
            "cache_dir": str(tmp_path),
            "scene_threshold": 0.30,
            "max_vision_calls": 2,
            "max_frames": 20,
            "vision_batch_size": 5,
        },
        "ffmpeg_bin": "/bin/echo",
    }
    url = "https://x.com/b"

    def fake_fetch_video(u, c):
        vp = str(tmp_path / "video.mp4")
        ap = str(tmp_path / "audio.m4a")
        with open(vp, "wb") as f:
            f.write(b"stub")
        with open(ap, "wb") as f:
            f.write(b"stub")
        return {"video_path": vp, "audio_path": ap, "duration_sec": 20.0, "tmp_dir": None}

    def fake_detect_cuts(video_path, threshold):
        return [5.0, 10.0, 15.0]

    def fake_extract_frames(video_path, times, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        out = []
        for i, t in enumerate(times):
            p = os.path.join(out_dir, "frame_{:03d}.png".format(i + 1))
            with open(p, "wb") as f:
                f.write(b"\x89PNG")
            out.append({"index": i + 1, "time": float(t), "path": p})
        return out

    def fake_vision_call(prompt, paths, timeout_sec=600):
        data = []
        for i, p in enumerate(paths):
            data.append({
                "index": i + 1, "path": p,
                "telop_text": "テロップ{}".format(i + 1),
                "telop_position": "top", "telop_color": "white",
                "telop_stroke": "", "emphasis_words": [], "size_class": "large",
                "visual_desc_en": "A person talks", "motion": "static",
                "has_person": True, "has_product_logo": False,
            })
        return {"ok": True, "data": data, "error": None}

    def fake_ametadata(ffmpeg_bin, audio_path, highpass_hz, window_samples=2048, timeout_sec=120):
        return ""  # オンセットなし

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 20.0, "segments": [{"start": 0.0, "end": 5.0, "text": "hi"}]}

    def fake_claude(prompt, timeout_sec=600):
        # ASR構成解析
        return {
            "ok": True,
            "data": {
                "beats": [
                    {"role": "hook", "start": 0.0, "end": 2.0, "text": "t", "summary": ""},
                    {"role": "cta", "start": 18.0, "end": 20.0, "text": "t", "summary": ""},
                ],
                "rhythm": {"sentence_count": 1, "avg_sentence_len": 5, "max_sentence_len": 5, "tone": "", "endings": []},
            },
        }

    def fake_fusion(prompt, timeout_sec=600):
        # LLM側は最低限を返す。矯正パスで cuts/sfx_events は上書きされる想定
        return {
            "ok": True,
            "data": {
                "version": 2,
                "url": url,
                "duration_sec": 20.0,
                "transcript": "て" * 40,
                "segments": [],
                "beats": [],
                "rhythm": None,
                "cuts": [{"t": 5.0, "confidence": 0.9}, {"t": 10.0, "confidence": 0.9}, {"t": 15.0, "confidence": 0.9}],
                "shots_ref": [{"index": 0, "start": 0.0, "end": 5.0, "visual_desc_en": "A person talks", "motion": "static"}],
                "telops": [{"start": 0.0, "end": 2.0, "text": "テロップ1", "position": "top",
                            "style": {"color": "white", "stroke": "", "size_class": "large"}, "emphasis_words": []}],
                "sfx_events": [],
                "bgm": {"present": False, "mood_guess": ""},
                "hook_end_sec": 2.0, "cta_start_sec": 18.0,
                "warnings": [],
            },
        }

    result = v2.analyze_reference_v2(
        url, cfg=cfg,
        fetch_video=fake_fetch_video,
        detect_cuts=fake_detect_cuts,
        extract_frames=fake_extract_frames,
        vision_call=fake_vision_call,
        ffmpeg_run_ametadata=fake_ametadata,
        asr_post=fake_asr,
        claude_call=fake_claude,
        fusion_call=fake_fusion,
    )
    assert result["ok"] is True, result
    spec = result["spec"]
    assert spec["version"] == 2
    assert len(spec["cuts"]) == 3
    assert spec["duration_sec"] == 20.0
    # キャッシュが書き込まれた
    cache_path = v2._cache_path_for_v2(ref_v1.normalize_url(url), cfg)
    assert os.path.exists(cache_path)


def test_analyze_reference_v2_synth_cuts_when_no_cuts(tmp_path):
    """カット検出0件時は 5秒等分の擬似カットが生成される。"""
    cfg = {"reference": {"cache_dir": str(tmp_path)}}

    def fake_fetch(u, c):
        vp = str(tmp_path / "video.mp4")
        ap = str(tmp_path / "audio.m4a")
        with open(vp, "wb") as f:
            f.write(b"stub")
        with open(ap, "wb") as f:
            f.write(b"stub")
        return {"video_path": vp, "audio_path": ap, "duration_sec": 20.0, "tmp_dir": None}

    def zero_cuts(video_path, threshold):
        return []

    def fake_extract(video_path, times, out_dir):
        return []

    def fake_ametadata(*a, **k):
        return ""

    def fake_asr(p, c):
        return {"ok": True, "text": "て" * 40, "duration": 20.0, "segments": []}

    def fake_claude(p, timeout_sec=600):
        return {"ok": True, "data": {"beats": [{"role": "hook", "start": 0, "end": 2, "text": "x", "summary": ""}],
                                       "rhythm": {"sentence_count": 1, "avg_sentence_len": 5,
                                                  "max_sentence_len": 5, "tone": "", "endings": []}}}

    def fake_fusion(p, timeout_sec=600):
        return {"ok": True, "data": {"version": 2, "cuts": [{"t": 5.0, "confidence": 0.5}], "telops": [], "sfx_events": []}}

    result = v2.analyze_reference_v2(
        "https://x.com/z", cfg=cfg,
        fetch_video=fake_fetch, detect_cuts=zero_cuts, extract_frames=fake_extract,
        vision_call=lambda *a, **k: {"ok": True, "data": []},
        ffmpeg_run_ametadata=fake_ametadata, asr_post=fake_asr,
        claude_call=fake_claude, fusion_call=fake_fusion,
    )
    assert result["ok"] is True
    assert any("擬似カット" in w for w in result["spec"].get("warnings") or [])


# ---------------------------------------------------------------------------
# (m) 3秒ごと密ストーリーボード解析（診断 P1-6 / 「3秒に1回、1枚1枚を忠実再現」）
# ---------------------------------------------------------------------------

def test_select_storyboard_times_uniform_interval():
    times = v2.select_storyboard_times(18.9, interval=3.0)
    # 中央狙い: 1.5, 4.5, 7.5, 10.5, 13.5, 16.5 → 6枚（≈3秒に1枚）
    assert times == [1.5, 4.5, 7.5, 10.5, 13.5, 16.5]


def test_select_storyboard_times_short_duration_single_center_frame():
    assert v2.select_storyboard_times(2.0, interval=3.0) == [1.0]
    assert v2.select_storyboard_times(0.0, interval=3.0) == []


def test_select_storyboard_times_caps_at_max_frames():
    times = v2.select_storyboard_times(600.0, interval=3.0, max_frames=10)
    assert len(times) == 10


def test_resolve_storyboard_enabled_rules():
    assert v2._resolve_storyboard_enabled({"reference": {"storyboard_enabled": True}}) is True
    assert v2._resolve_storyboard_enabled({"reference": {"storyboard_enabled": False},
                                           "director_quality": "supreme_plus"}) is False  # 明示優先
    assert v2._resolve_storyboard_enabled({"director_quality": "supreme_plus"}) is True
    assert v2._resolve_storyboard_enabled({"director_quality": "ttps"}) is True
    assert v2._resolve_storyboard_enabled({"director_quality": "supreme"}) is False
    assert v2._resolve_storyboard_enabled({}) is False


def test_analyze_storyboard_builds_entries_from_vision():
    frames = [
        {"index": 1, "time": 1.5, "path": "/f/1.png"},
        {"index": 2, "time": 4.5, "path": "/f/2.png"},
    ]

    def fake_vision(prompt, paths, timeout_sec=600):
        return {"ok": True, "data": [
            {"index": 1, "description_ja": "男性が頬に指を当てている", "on_screen_text": "10%/100%",
             "has_person": True, "person_desc": "髭のある成人男性", "objects": ["指", "顔"]},
            {"index": 2, "description_ja": "白背景に商品ボトル", "on_screen_text": "100%/100%",
             "has_person": False, "person_desc": "", "objects": ["ボトル", "箱", "余分", "多", "すぎ", "る7", "cut"]},
        ]}

    results, warns = v2.analyze_storyboard(frames, cfg={"reference": {}}, vision_call=fake_vision)
    assert len(results) == 2
    assert results[0]["t"] == 1.5
    assert results[0]["on_screen_text"] == "10%/100%"
    assert results[0]["has_person"] is True
    assert results[0]["person_desc"] == "髭のある成人男性"
    # objects は最大6件に切り詰め
    assert len(results[1]["objects"]) == 6
    assert warns == []


def test_analyze_storyboard_respects_max_vision_calls():
    frames = [{"index": i + 1, "time": float(i), "path": "/f/{}.png".format(i)} for i in range(30)]
    calls = {"n": 0}

    def fake_vision(prompt, paths, timeout_sec=600):
        calls["n"] += 1
        return {"ok": True, "data": [
            {"index": j + 1, "description_ja": "x", "on_screen_text": "", "has_person": False,
             "person_desc": "", "objects": []} for j in range(len(paths))
        ]}

    cfg = {"reference": {"storyboard_batch_size": 5, "storyboard_max_vision_calls": 2}}
    results, warns = v2.analyze_storyboard(frames, cfg=cfg, vision_call=fake_vision)
    assert calls["n"] == 2              # 上限で打ち切り
    assert len(results) == 10           # 5枚×2バッチ
    assert any("上限" in w for w in warns)


def test_analyze_storyboard_vision_unavailable_degrades(monkeypatch):
    # vision_call=None は「既定 CLI を解決」の意味なので、CLI 自体が無い状況を再現する
    monkeypatch.setattr(v2, "call_claude_vision_json", None)
    frames = [{"index": 1, "time": 1.5, "path": "/f/1.png"}]
    results, warns = v2.analyze_storyboard(frames, cfg={"reference": {}}, vision_call=None)
    assert results == []
    assert any("vision" in w for w in warns)


def test_validate_v2_accepts_optional_storyboard():
    spec = _minimal_valid_v2()
    spec["storyboard"] = [
        {"t": 1.5, "description_ja": "男性", "on_screen_text": "10%", "has_person": True,
         "person_desc": "男", "objects": ["指"]},
        {"t": 4.5, "description_ja": "商品", "on_screen_text": "", "has_person": False,
         "person_desc": "", "objects": []},
    ]
    ok, errors, norm = v2.validate_reference_spec_v2(spec)
    assert ok is True, errors
    assert len(norm["storyboard"]) == 2


def test_validate_v2_rejects_malformed_storyboard():
    spec = _minimal_valid_v2()
    spec["storyboard"] = [{"t": "notnum", "on_screen_text": 123}]
    ok, errors, _ = v2.validate_reference_spec_v2(spec)
    assert ok is False
    assert any("storyboard" in e for e in errors)


def test_analyze_reference_v2_attaches_storyboard_when_supreme_plus(tmp_path):
    """storyboard が supreme_plus で自動 ON になり、spec に付与されることを stub 経由で検証。"""
    cfg = {
        "director_quality": "supreme_plus",
        "reference": {"cache_dir": str(tmp_path), "storyboard_interval_sec": 3.0,
                      "max_vision_calls": 2, "max_frames": 20, "vision_batch_size": 5},
        "ffmpeg_bin": "/bin/echo",
    }
    url = "https://x.com/sb"

    def fake_fetch(u, c):
        vp = str(tmp_path / "video.mp4"); ap = str(tmp_path / "audio.m4a")
        for p in (vp, ap):
            with open(p, "wb") as f:
                f.write(b"stub")
        return {"video_path": vp, "audio_path": ap, "duration_sec": 12.0, "tmp_dir": None}

    def fake_cuts(video_path, threshold):
        return [4.0, 8.0]

    def fake_extract(video_path, times, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        out = []
        for i, t in enumerate(times):
            p = os.path.join(out_dir, "f_{:03d}.png".format(i + 1))
            with open(p, "wb") as f:
                f.write(b"\x89PNG")
            out.append({"index": i + 1, "time": float(t), "path": p})
        return out

    def fake_vision(prompt, paths, timeout_sec=600):
        # storyboard プロンプトなら storyboard スキーマ、通常なら vision スキーマを返す
        if "description_ja" in prompt:
            return {"ok": True, "data": [
                {"index": i + 1, "description_ja": "説明{}".format(i + 1),
                 "on_screen_text": "{}%".format((i + 1) * 10), "has_person": True,
                 "person_desc": "男性", "objects": ["物"]} for i in range(len(paths))
            ]}
        return {"ok": True, "data": [
            {"index": i + 1, "path": p, "telop_text": "t{}".format(i + 1), "telop_position": "top",
             "telop_color": "white", "telop_stroke": "", "emphasis_words": [], "size_class": "large",
             "visual_desc_en": "A person talks", "motion": "static", "has_person": True,
             "has_product_logo": False} for i, p in enumerate(paths)
        ]}

    def fake_ametadata(*a, **k):
        return ""

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 20, "duration": 12.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {"ok": True, "data": {"beats": [], "rhythm": None}}

    def fake_fusion(prompt, timeout_sec=600):
        return {"ok": True, "data": {
            "version": 2, "url": url, "duration_sec": 12.0, "transcript": "て" * 20,
            "segments": [], "beats": [], "rhythm": None,
            "cuts": [{"t": 4.0, "confidence": 0.9}], "shots_ref": [],
            "telops": [], "sfx_events": [], "bgm": {"present": False, "mood_guess": ""}, "warnings": [],
        }}

    result = v2.analyze_reference_v2(
        url, cfg=cfg, fetch_video=fake_fetch, detect_cuts=fake_cuts, extract_frames=fake_extract,
        vision_call=fake_vision, ffmpeg_run_ametadata=fake_ametadata, asr_post=fake_asr,
        claude_call=fake_claude, fusion_call=fake_fusion,
    )
    assert result["ok"] is True, result
    spec = result["spec"]
    # duration 12s / interval 3s → 中央狙い 4枚（1.5,4.5,7.5,10.5）
    assert "storyboard" in spec
    assert len(spec["storyboard"]) == 4
    assert spec["storyboard"][0]["on_screen_text"] == "10%"
    assert result["meta"]["storyboard_entries"] == 4


def test_analyze_reference_v2_cache_bypassed_when_storyboard_requested_but_absent(tmp_path):
    """codex-review P2: storyboard 要求時、storyboard を持たない旧 cache は再解析される。"""
    cfg_base = {"reference": {"cache_dir": str(tmp_path)}}
    url = "https://x.com/p2"
    cache_path = v2._cache_path_for_v2(ref_v1.normalize_url(url), cfg_base)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    # storyboard キーを持たない旧 cache を用意
    old_spec = {"version": 2, "url": url, "duration_sec": 12.0, "cuts": []}
    with open(cache_path, "w", encoding="utf-8") as f:
        import json as _j
        _j.dump(old_spec, f)

    # storyboard 無効なら cache をそのまま返す
    res_off = v2.analyze_reference_v2(url, cfg={"reference": {"cache_dir": str(tmp_path)}})
    assert res_off["cached"] is True and res_off["source"] == "cache"

    # storyboard 有効（supreme_plus）だと storyboard を持たない cache は使わず再解析へ進む
    # （ここでは fetch を失敗させ、cache を返さず解析経路に入ったことを error で確認する）。
    def boom_fetch(u, c):
        raise RuntimeError("re-analyze path reached")
    res_on = v2.analyze_reference_v2(
        url, cfg={"director_quality": "supreme_plus", "reference": {"cache_dir": str(tmp_path)}},
        fetch_video=boom_fetch,
    )
    assert res_on["cached"] is False
    assert "re-analyze path reached" in (res_on["error"] or "")


def test_analyze_reference_v2_cache_used_when_storyboard_key_present(tmp_path):
    """storyboard キーを既に持つ cache は storyboard 有効でもそのまま再利用する（再解析しない）。"""
    cfg = {"director_quality": "supreme_plus", "reference": {"cache_dir": str(tmp_path)}}
    url = "https://x.com/p2b"
    cache_path = v2._cache_path_for_v2(ref_v1.normalize_url(url), cfg)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    spec = {"version": 2, "url": url, "duration_sec": 12.0, "cuts": [], "storyboard": []}
    with open(cache_path, "w", encoding="utf-8") as f:
        import json as _j
        _j.dump(spec, f)
    res = v2.analyze_reference_v2(url, cfg=cfg)
    assert res["cached"] is True and res["source"] == "cache"
