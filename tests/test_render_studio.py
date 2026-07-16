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
    """既存のBGM無しパス（sfx未指定）が従来どおり[premix]経由で[aout]まで到達する。"""
    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0,
    )
    joined = " ".join(cmd)
    assert "apad=whole_dur=10.000" in joined
    assert "[premix]" in joined
    assert "[aout]" in cmd or "[aout]" in joined


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
    assert "amix=inputs=3:duration=longest:normalize=0[premix2]" in joined
    assert "[premix2]" in joined


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
    assert "amix=inputs=2:duration=longest:normalize=0[premix2]" in joined


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
    assert "amix=inputs=2:duration=longest:normalize=0[premix]" in " ".join(cmd_no_ducking)


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
