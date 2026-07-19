# -*- coding: utf-8 -*-
"""pipeline/render.py の edit enhancement層（カット点SE/パンチイン/BGM音量カーブ/
フックのインパクト）: フィルタ文字列・イベント列構築の純関数テスト。実ffmpegは使わない
（実ffmpeg実行のE2Eは tests/test_render_edit_enhancement_e2e.py に分離）。
"""
from pipeline import render


# ---------------------------------------------------------------------------
# compute_cut_sfx_events / build_edit_cut_sfx_specs
# ---------------------------------------------------------------------------

def test_compute_cut_sfx_events_skips_first_boundary():
    """1番目のショット開始(0.0)は「カット」ではないため除外される。"""
    events = render.compute_cut_sfx_events([0.0, 3.0, 6.0], min_interval_sec=1.5)
    assert events == [3.0, 6.0]


def test_compute_cut_sfx_events_thins_close_boundaries_by_min_interval():
    events = render.compute_cut_sfx_events([0.0, 3.0, 3.8, 6.0], min_interval_sec=1.5)
    # 3.8は直前採用(3.0)から0.8秒しか離れておらず間引かれる
    assert events == [3.0, 6.0]


def test_compute_cut_sfx_events_empty_for_single_or_no_shot():
    assert render.compute_cut_sfx_events([0.0], min_interval_sec=1.5) == []
    assert render.compute_cut_sfx_events([], min_interval_sec=1.5) == []


def test_build_edit_cut_sfx_specs_disabled_returns_empty():
    profile = {"cut_sfx": {"enabled": False, "resolved_path": "/a/whoosh.wav", "gain_db": -18, "min_interval_sec": 1.5}}
    assert render.build_edit_cut_sfx_specs([0.0, 3.0], profile) == []


def test_build_edit_cut_sfx_specs_missing_resolved_path_returns_empty():
    profile = {"cut_sfx": {"enabled": True, "resolved_path": None, "gain_db": -18, "min_interval_sec": 1.5}}
    assert render.build_edit_cut_sfx_specs([0.0, 3.0], profile) == []


def test_build_edit_cut_sfx_specs_builds_path_at_sec_gain():
    profile = {"cut_sfx": {"enabled": True, "resolved_path": "/assets/sfx/whoosh_01.wav", "gain_db": -18, "min_interval_sec": 1.5}}
    specs = render.build_edit_cut_sfx_specs([0.0, 3.0, 6.0], profile)
    assert specs == [
        {"path": "/assets/sfx/whoosh_01.wav", "at_sec": 3.0, "gain_db": -18},
        {"path": "/assets/sfx/whoosh_01.wav", "at_sec": 6.0, "gain_db": -18},
    ]


# ---------------------------------------------------------------------------
# build_punch_in_zoompan_filter / resolve_punch_in_filter_for_shot
# ---------------------------------------------------------------------------

def test_build_punch_in_zoompan_filter_static_returns_none():
    assert render.build_punch_in_zoompan_filter("static", 1.08, 3.0) is None
    assert render.build_punch_in_zoompan_filter(None, 1.08, 3.0) is None


def test_build_punch_in_zoompan_filter_unknown_pattern_returns_none():
    assert render.build_punch_in_zoompan_filter("bogus_pattern", 1.08, 3.0) is None


def test_build_punch_in_zoompan_filter_zoom_in_slow_contains_zoompan_and_max_zoom():
    filt = render.build_punch_in_zoompan_filter("zoom_in_slow", 1.08, 3.0, fps=30)
    assert filt.startswith("zoompan=")
    assert "1.0800" in filt
    assert "fps=30" in filt
    assert "s=1080x1920" in filt


def test_build_punch_in_zoompan_filter_zoom_in_fast_reaches_max_zoom_faster_than_slow():
    slow = render.build_punch_in_zoompan_filter("zoom_in_slow", 1.08, 3.0, fps=30)
    fast = render.build_punch_in_zoompan_filter("zoom_in_fast", 1.08, 3.0, fps=30)
    # zoom_in_fastは前半40%フレームで到達するため、1フレームあたりのstepがslowより大きい。
    slow_step = float(slow.split("zoom+")[1].split(",")[0])
    fast_step = float(fast.split("zoom+")[1].split(",")[0])
    assert fast_step > slow_step


def test_build_punch_in_zoompan_filter_pan_subtle_has_pan_expression():
    filt = render.build_punch_in_zoompan_filter("pan_subtle", 1.08, 3.0, fps=30)
    assert "zoompan=" in filt
    assert "on/" in filt  # xの時間依存パン式


def test_resolve_punch_in_filter_for_shot_disabled_returns_none():
    profile = {"punch_in": {"enabled": False, "pattern": ["zoom_in_slow"], "max_zoom": 1.08, "skip_if_backend": "mock"}}
    assert render.resolve_punch_in_filter_for_shot(0, 3.0, profile, "higgsfield") is None


def test_resolve_punch_in_filter_for_shot_skips_configured_backend():
    profile = {"punch_in": {"enabled": True, "pattern": ["zoom_in_slow"], "max_zoom": 1.08, "skip_if_backend": "mock"}}
    assert render.resolve_punch_in_filter_for_shot(0, 3.0, profile, "mock") is None


def test_resolve_punch_in_filter_for_shot_applies_for_non_skipped_backend():
    profile = {"punch_in": {"enabled": True, "pattern": ["zoom_in_slow"], "max_zoom": 1.08, "skip_if_backend": "mock"}}
    filt = render.resolve_punch_in_filter_for_shot(0, 3.0, profile, "higgsfield")
    assert filt is not None and filt.startswith("zoompan=")


def test_resolve_punch_in_filter_for_shot_cycles_pattern_by_index():
    profile = {
        "punch_in": {
            "enabled": True, "pattern": ["zoom_in_slow", "static", "zoom_in_fast"],
            "max_zoom": 1.08, "skip_if_backend": "mock",
        }
    }
    # index1 -> "static" -> None、index3(=0巡目に戻る) -> "zoom_in_slow" -> フィルタあり
    assert render.resolve_punch_in_filter_for_shot(1, 3.0, profile, "higgsfield") is None
    filt_3 = render.resolve_punch_in_filter_for_shot(3, 3.0, profile, "higgsfield")
    filt_0 = render.resolve_punch_in_filter_for_shot(0, 3.0, profile, "higgsfield")
    assert filt_3 == filt_0


# ---------------------------------------------------------------------------
# build_bgm_curve_volume_filter / build_first_shot_impact_filter
# ---------------------------------------------------------------------------

def test_build_bgm_curve_volume_filter_none_matches_legacy_fixed_volume():
    """bgm_curve=Noneのときは従来の固定volume文字列と完全一致する（後方互換）。"""
    fallback = render.gain_db_to_linear(-8)
    assert render.build_bgm_curve_volume_filter(None, fallback) == "volume={:.4f}".format(fallback)


def test_build_bgm_curve_volume_filter_with_curve_builds_conditional_expression():
    bgm_curve = {
        "hook_end_sec": 2.0, "cta_start_sec": 8.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12,
    }
    filt = render.build_bgm_curve_volume_filter(bgm_curve, render.gain_db_to_linear(-8))
    assert filt.startswith("volume='if(lt(t,2.000)")
    assert "gte(t,8.000)" in filt
    assert filt.endswith("':eval=frame")


# ---------------------------------------------------------------------------
# build_first_shot_impact_filter
# ---------------------------------------------------------------------------

def test_build_first_shot_impact_filter_contains_zoompan_and_scale_pop_values():
    filt = render.build_first_shot_impact_filter(duration_sec=0.3, start_scale=1.04, fps=30)
    assert filt.startswith("zoompan=")
    assert "1.0400" in filt
    assert "fps=30" in filt


# ---------------------------------------------------------------------------
# compute_edit_enhancement_kwargs
# ---------------------------------------------------------------------------

def _full_edit_profile(**overrides):
    from pipeline import edit_profile as ep
    profile = ep._merge_edit_profile(overrides or None)
    profile["cut_sfx"]["resolved_path"] = "/assets/sfx/whoosh_01.wav"
    return profile


def test_compute_edit_enhancement_kwargs_empty_shots_returns_safe_defaults():
    profile = _full_edit_profile()
    kwargs = render.compute_edit_enhancement_kwargs([], profile)
    assert kwargs == {"sfx_extra": [], "bgm_curve": None, "first_shot_impact_sec": None}


def test_compute_edit_enhancement_kwargs_builds_bgm_curve_boundaries_from_durations():
    profile = _full_edit_profile()
    kwargs = render.compute_edit_enhancement_kwargs([2.0, 3.0, 2.0, 4.0], profile)
    # hook_end = 2番目のショット開始(=1番目の尺2.0) / cta_start = 最後のショット開始(2+3+2=7.0)
    assert kwargs["bgm_curve"]["hook_end_sec"] == 2.0
    assert kwargs["bgm_curve"]["cta_start_sec"] == 7.0
    assert kwargs["bgm_curve"]["fade_out_sec"] == 1.5


def test_compute_edit_enhancement_kwargs_sfx_extra_matches_cut_events():
    profile = _full_edit_profile()
    kwargs = render.compute_edit_enhancement_kwargs([2.0, 3.0, 2.0], profile)
    at_secs = [s["at_sec"] for s in kwargs["sfx_extra"]]
    assert at_secs == [2.0, 5.0]


def test_compute_edit_enhancement_kwargs_first_shot_impact_capped_to_first_shot_duration():
    profile = _full_edit_profile()
    kwargs = render.compute_edit_enhancement_kwargs([0.15, 3.0], profile)
    assert kwargs["first_shot_impact_sec"] == 0.15  # 最初のショットが0.3秒未満ならそれに合わせる


def test_compute_edit_enhancement_kwargs_all_disabled_returns_empty():
    profile = _full_edit_profile()
    profile["cut_sfx"]["enabled"] = False
    profile["bgm_curve"]["enabled"] = False
    profile["first_shot_impact"]["enabled"] = False
    kwargs = render.compute_edit_enhancement_kwargs([2.0, 3.0], profile)
    assert kwargs == {"sfx_extra": [], "bgm_curve": None, "first_shot_impact_sec": None}


# ---------------------------------------------------------------------------
# build_normalize_clip_cmd(punch_in_filter=...) / build_final_cmd(bgm_curve=..., first_shot_impact_sec=...)
# 後方互換性・統合の検証
# ---------------------------------------------------------------------------

def test_build_normalize_clip_cmd_without_punch_in_filter_is_unchanged():
    cmd = render.build_normalize_clip_cmd("/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0)
    joined = " ".join(cmd)
    assert "zoompan" not in joined


def test_build_normalize_clip_cmd_appends_punch_in_filter_to_vf_chain():
    punch_in = render.build_punch_in_zoompan_filter("zoom_in_slow", 1.08, 3.0)
    cmd = render.build_normalize_clip_cmd(
        "/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=3.0, punch_in_filter=punch_in
    )
    vf_value = cmd[cmd.index("-vf") + 1]
    assert vf_value.endswith(punch_in)
    assert vf_value.startswith("scale=1080:1920")


def test_build_final_cmd_without_edit_kwargs_matches_legacy_output():
    """bgm_curve/first_shot_impact_secを渡さない呼び出しは、edit機能追加前と完全に同じコマンドになる。"""
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=10.0, bgm_gain_db=-8,
    )
    joined = " ".join(cmd)
    assert "volume=0.3981" in joined  # gain_db_to_linear(-8) の固定値（従来どおり）
    assert "d=2.000" in joined  # fade_out_secデフォルト2秒（従来どおり）
    assert "zoompan" not in joined
    assert "if(lt(t," not in joined


def test_build_final_cmd_with_bgm_curve_uses_conditional_volume_and_custom_fade():
    bgm_curve = {
        "hook_end_sec": 2.0, "cta_start_sec": 8.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12, "fade_out_sec": 1.5,
    }
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=10.0, bgm_curve=bgm_curve,
    )
    joined = " ".join(cmd)
    assert "if(lt(t,2.000)" in joined
    assert "d=1.500" in joined  # fade_out_sec=1.5が反映される


def test_build_final_cmd_with_first_shot_impact_prefixes_zoompan_before_subtitles():
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0, first_shot_impact_sec=0.3,
    )
    joined = " ".join(cmd)
    assert "[0:v]zoompan=" in joined
    subs_idx = joined.index("subtitles=filename=")
    zoompan_idx = joined.index("zoompan=")
    assert zoompan_idx < subs_idx  # zoompanが字幕焼込より先に適用される


def test_build_final_cmd_with_sfx_and_bgm_curve_together_has_both():
    bgm_curve = {"hook_end_sec": 1.0, "cta_start_sec": 5.0, "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12}
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=10.0, bgm_curve=bgm_curve,
        sfx=[{"path": "/assets/sfx/whoosh_01.wav", "at_sec": 3.0, "gain_db": -18}],
    )
    joined = " ".join(cmd)
    assert "if(lt(t,1.000)" in joined
    assert "adelay=3000|3000" in joined
    assert "/assets/sfx/whoosh_01.wav" in cmd
