# -*- coding: utf-8 -*-
"""Reel Studio向けのpipeline/render.py拡張（trim/BGM gain_db/SFXオーバーレイ）の単体テスト。

既存のtests/test_render.py（後方互換の維持）は無改修。ここでは追加パラメータの
挙動のみを検証する（純関数・ffmpeg実行なし）。
"""
from pipeline import render


def test_build_normalize_clip_cmd_without_trim_start_is_unchanged():
    cmd = render.build_normalize_clip_cmd("/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0)
    assert cmd[0] == "/bin/ffmpeg"
    assert "-ss" not in cmd


def test_build_normalize_clip_cmd_with_trim_start_inserts_ss_before_input():
    cmd = render.build_normalize_clip_cmd(
        "/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=2.5, trim_start=1.2
    )
    assert cmd[0] == "/bin/ffmpeg"
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx
    assert cmd[ss_idx + 1] == "1.200"
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "2.500"


def test_gain_db_to_linear_zero_db_is_unity_gain():
    assert abs(render.gain_db_to_linear(0) - 1.0) < 1e-9


def test_gain_db_to_linear_negative_attenuates():
    assert render.gain_db_to_linear(-14) < 1.0
    assert render.gain_db_to_linear(-6) > render.gain_db_to_linear(-14)


def test_build_sfx_overlay_filters_produces_adelay_and_labels():
    sfx = [{"path": "/a.wav", "at_sec": 2.5, "gain_db": -6}, {"path": "/b.wav", "at_sec": 0, "gain_db": 0}]
    parts, labels = render.build_sfx_overlay_filters(sfx, start_index=3)
    assert len(parts) == 2 and len(labels) == 2
    assert "[3:a]" in parts[0] and "adelay=2500|2500" in parts[0]
    assert "[4:a]" in parts[1] and "adelay=0|0" in parts[1]
    assert labels == ["[sfx0]", "[sfx1]"]


def test_build_final_cmd_without_bgm_and_without_sfx_backward_compat():
    """BGM無し・SFX無しの経路: [main_mix]→loudnorm→[main_loud_raw]→anull→[aout] のシンプル経路になる(BUG-53修正後)."""
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0,
    )
    joined = " ".join(cmd)
    assert "apad=whole_dur=10.000" in joined
    assert "[main_mix]" in joined
    assert "[main_loud_raw]" in joined
    assert "[aout]" in joined


def test_build_final_cmd_with_sfx_adds_extra_inputs_and_amix():
    sfx = [{"path": "/whoosh.wav", "at_sec": 1.0, "gain_db": -6}, {"path": "/ding.wav", "at_sec": 3.0, "gain_db": -8}]
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=8.0, sfx=sfx,
    )
    assert "/whoosh.wav" in cmd and "/ding.wav" in cmd
    joined = " ".join(cmd)
    assert "adelay=1000|1000" in joined
    assert "adelay=3000|3000" in joined
    # BUG-53: SFX は loudnorm 後段 ([main_loud]) にオーバーレイし [final_mix] へ集約 → alimiter → [aout]
    assert "[main_loud][sfx0][sfx1]amix=inputs=3:duration=longest:normalize=0[final_mix]" in joined
    # BUG-53: alimiter は SE transient を release で潰すため asoftclip に置換済み
    assert "asoftclip" in joined
    assert "[final_mix]asoftclip" in joined


def test_build_final_cmd_with_bgm_and_sfx_indices_after_bgm():
    sfx = [{"path": "/whoosh.wav", "at_sec": 0.5, "gain_db": -6}]
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=8.0, sfx=sfx,
    )
    joined = " ".join(cmd)
    assert "sidechaincompress" in joined
    # 入力順: 0=concat,1=narration,2=bgm,3=sfx0
    assert "[3:a]" in joined
    # SFX overlay is post-loudnorm (BUG-53): amix with [main_loud] as the first input.
    assert "[main_loud][sfx0]amix=inputs=2:duration=longest:normalize=0[final_mix]" in joined
    # BUG-53: alimiter は SE transient を release で潰すため asoftclip(=RMSに影響しないピーク丸め)に置換
    assert "asoftclip" in joined


def test_build_final_cmd_bgm_ducking_false_skips_sidechaincompress():
    cmd_ducking = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=8.0, ducking=True,
    )
    cmd_no_ducking = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=8.0, ducking=False,
    )
    assert "sidechaincompress" in " ".join(cmd_ducking)
    assert "sidechaincompress" not in " ".join(cmd_no_ducking)
    # BUG-53: main ミックス経路の最終ラベルは [main_mix]（loudnorm 前）に統一。
    assert "amix=inputs=2:duration=longest:normalize=0[main_mix]" in " ".join(cmd_no_ducking)


def test_build_final_cmd_loudnorm_applied_before_sfx_overlay():
    """BUG-53: loudnorm は本編ミックス(main_mix)のみに掛けたのち SFX を後段でオーバーレイする。
    SFX が amix chain 内で loudnorm より前に来ないことをコマンド文字列で担保する。
    """
    sfx = [{"path": "/whoosh.wav", "at_sec": 1.0, "gain_db": -6}]
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=8.0, sfx=sfx,
    )
    joined = " ".join(cmd)
    # loudnorm は [main_mix] → [main_loud] にだけ掛かる
    idx_loud_apply = joined.index("[main_mix]loudnorm")
    idx_sfx_overlay = joined.index("[main_loud][sfx0]")
    assert idx_loud_apply < idx_sfx_overlay


def test_build_bgm_curve_volume_filter_with_dip_events_reduces_bgm_around_sfx():
    """BUG-53: bgm_curve.dip_events があると SE時刻±0.3s(既定)で BGM を -4dB 追加ダッキングする。"""
    curve = {
        "hook_end_sec": 3.0, "cta_start_sec": 15.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12,
        "dip_events": [5.0, 10.0],
    }
    flt = render.build_bgm_curve_volume_filter(curve, 0.5)
    # -4dB を線形係数へ換算
    expected_dip = render.gain_db_to_linear(render.SFX_DIP_GAIN_DB)
    assert "{:.4f}".format(expected_dip) in flt
    assert "between(t,4.700,5.300)" in flt
    assert "between(t,9.700,10.300)" in flt


def test_build_bgm_curve_volume_filter_no_dip_events_backward_compat():
    """dip_events 未指定なら従来どおり単純な hook/body/cta の三段volume式のまま。"""
    curve = {
        "hook_end_sec": 3.0, "cta_start_sec": 15.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12,
    }
    flt = render.build_bgm_curve_volume_filter(curve, 0.5)
    assert "between(t" not in flt
    assert "if(lt(t,3.000)" in flt


def test_compute_edit_enhancement_kwargs_populates_bgm_dip_events():
    """BUG-53: sfx_extra があれば bgm_curve.dip_events に at_sec を写す。"""
    durations = [2.0, 3.0, 4.0]
    edit_profile = {
        "bgm_curve": {"enabled": True, "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12},
        "cut_sfx": {"enabled": False},  # cut_sfx は無効(SEはplan v1経路で生成しない)
    }
    result = render.compute_edit_enhancement_kwargs(durations, edit_profile)
    # cut_sfx=disabled かつ plan 無しなら sfx_extra=[] のはず → dip_events は生えない
    assert result["sfx_extra"] == []
    assert result["bgm_curve"] is not None
    assert "dip_events" not in result["bgm_curve"]


def test_build_main_dip_volume_filter_produces_between_windows():
    """BUG-53: main_dip_events を渡すと SE時刻±MAIN_DIP_HALF_WINDOW_SEC の窓だけ MAIN_DIP_GAIN_DB のダッキングを掛ける。"""
    flt = render.build_main_dip_volume_filter([3.0, 6.5])
    half = render.MAIN_DIP_HALF_WINDOW_SEC
    assert "between(t,{:.3f},{:.3f})".format(3.0 - half, 3.0 + half) in flt
    assert "between(t,{:.3f},{:.3f})".format(6.5 - half, 6.5 + half) in flt
    expected = render.gain_db_to_linear(render.MAIN_DIP_GAIN_DB)
    assert "{:.4f}".format(expected) in flt


def test_build_main_dip_volume_filter_empty_is_passthrough():
    flt = render.build_main_dip_volume_filter([])
    assert flt == "volume=1.0"
    flt2 = render.build_main_dip_volume_filter(None)
    assert flt2 == "volume=1.0"


def test_build_final_cmd_applies_main_dip_only_when_sfx_present():
    """SFXがあり main_dip_events を渡すと main_loud_raw → main_loud に dip の volume 段が挿入される。"""
    sfx = [{"path": "/x.wav", "at_sec": 2.0, "gain_db": -6}]
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=5.0, sfx=sfx, main_dip_events=[2.0],
    )
    joined = " ".join(cmd)
    # dip 適用: [main_loud_raw]volume=...[main_loud]
    assert "[main_loud_raw]" in joined
    half = render.MAIN_DIP_HALF_WINDOW_SEC
    assert "between(t,{:.3f},{:.3f})".format(2.0 - half, 2.0 + half) in joined
    # dip 適用後の [main_loud] を SFX と amix
    assert "[main_loud][sfx0]amix=inputs=2:duration=longest:normalize=0[final_mix]" in joined


def test_build_final_cmd_no_sfx_skips_main_dip_stage():
    """SFXが無い場合は dip 段を挟まず [main_loud_raw]→anull→[aout]。"""
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=5.0,
    )
    joined = " ".join(cmd)
    assert "[main_loud_raw]anull[aout]" in joined
    assert "between(t," not in joined


def test_compute_edit_enhancement_kwargs_main_dip_events_matches_sfx_times():
    """BUG-53: main_dip_events は sfx_extra の at_sec 列と一致する。"""
    durations = [2.0, 3.0, 4.0]
    edit_profile = {
        "bgm_curve": {"enabled": True, "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12},
        "cut_sfx": {"enabled": True, "resolved_path": "/tmp/dummy_sfx.wav", "min_interval_sec": 0.5,
                    "gain_db": -6},
    }
    result = render.compute_edit_enhancement_kwargs(durations, edit_profile)
    assert result["sfx_extra"], "sfx_extra should be populated"
    assert "main_dip_events" in result
    assert result["main_dip_events"] == [s["at_sec"] for s in result["sfx_extra"]]


def test_compute_edit_enhancement_kwargs_dip_events_present_when_sfx_extra():
    """SE が実際に配置されるときは dip_events が bgm_curve に載る。"""
    # 手動 sfx_extra を注入するテスト経路として、resolved_path 固定モードを使う
    durations = [2.0, 3.0, 4.0]
    edit_profile = {
        "bgm_curve": {"enabled": True, "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12},
        "cut_sfx": {"enabled": True, "resolved_path": "/tmp/dummy_sfx.wav", "min_interval_sec": 0.5,
                    "gain_db": -8},
    }
    result = render.compute_edit_enhancement_kwargs(durations, edit_profile)
    assert result["sfx_extra"], "sfx_extra should be non-empty for this configuration"
    assert result["bgm_curve"] is not None
    assert "dip_events" in result["bgm_curve"]
    assert len(result["bgm_curve"]["dip_events"]) == len(result["sfx_extra"])


def test_build_final_cmd_bgm_gain_db_overrides_default_volume():
    cmd_default = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=8.0,
    )
    cmd_custom = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path="/bgm.mp3", out_duration=8.0, bgm_gain_db=-20,
    )
    joined_default = " ".join(cmd_default)
    joined_custom = " ".join(cmd_custom)
    assert "volume=0.5500" in joined_default
    assert "volume={:.4f}".format(render.gain_db_to_linear(-20)) in joined_custom
    assert joined_default != joined_custom
