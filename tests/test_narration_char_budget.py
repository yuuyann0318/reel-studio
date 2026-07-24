# -*- coding: utf-8 -*-
"""narration_jp 文字数バジェット検査のテスト（F13）。

参考動画のリズム再現のため、各 shot の narration_jp 文字数を
「expected_narration_chars（無ければ 尺 × chars_per_sec 既定 6.5）× (1 + 15%)」で拘束する。
超過するとバリデータが不合格エラーを返し、矯正リトライ対象となる。

同時に、render.apply_tempo_guard() が音声実尺を計画尺にatempo(<=1.15)で圧縮するテストも置く。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from pipeline import director, render


# ---------------------------------------------------------------------------
# 文字数バジェット検査（_validate_plan_matches_skeleton）
# ---------------------------------------------------------------------------

def _minimal_skeleton(chars_per_sec=6.0, expected_chars=None):
    """バジェット検査だけを試したい最小 skeleton を組む（他検査は素通り）。"""
    shot = {
        "id": "s1",
        "duration_sec": 4.0,
        "motion_preset": "static",
    }
    if expected_chars is not None:
        shot["expected_narration_chars"] = int(expected_chars)
    skel = {
        "shots": [shot],
        "sfx_plan": [],
        "hook_end_shot_id": "s1",
        "cta_start_shot_id": "s1",
    }
    if chars_per_sec is not None:
        skel["narration_rhythm"] = {
            "chars_per_sec": float(chars_per_sec),
            "avg_gap_sec": 0.0,
            "pause_points": [],
        }
    return skel


def _plan_for(skel, narration_jp):
    shot = dict(skel["shots"][0])
    shot["visual_prompt"] = "x"
    shot["caption_jp"] = ""
    shot["narration_jp"] = narration_jp
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": narration_jp,
        "shots": [shot], "bgm_mood": "upbeat",
        "sfx_plan": [],
        "hook_end_shot_id": "s1", "cta_start_shot_id": "s1",
    }


def test_char_budget_rejects_overrun_from_expected_chars():
    """expected_narration_chars=15 なら 15*1.15=17字までは許容、18字以上で不合格。"""
    skel = _minimal_skeleton(expected_chars=15)
    # 17字（許容内）
    ok_plan = _plan_for(skel, "あ" * 17)
    errs = director._validate_plan_matches_skeleton(ok_plan, skel, target_duration_sec=4.0)
    assert not any("narration_jp" in e and ("文字数" in e or "字" in e) for e in errs), errs
    # 18字（許容外）
    ng_plan = _plan_for(skel, "あ" * 18)
    errs = director._validate_plan_matches_skeleton(ng_plan, skel, target_duration_sec=4.0)
    assert any("narration_jp" in e for e in errs), errs


def test_char_budget_falls_back_to_default_chars_per_sec():
    """rhythm 未載＋expected_narration_chars 無しでも既定 6.5字/秒 × 尺 × 1.15 で拘束する。

    ★意図: 参考動画に transcript が無く narration_rhythm.chars_per_sec=0 の
    E2E ケース（実例あり）でも「長すぎる narration_jp → TTS 尺伸び → shot 引き伸ばし」
    の連鎖を防ぐ。既定値は Kyoko/日本語話者の一般的な音読速度レンジ。
    """
    skel = _minimal_skeleton(chars_per_sec=None, expected_chars=None)
    # 尺 4.0秒 → budget 26, 許容 29 → 40字は不合格
    ng_plan = _plan_for(skel, "あ" * 40)
    errs = director._validate_plan_matches_skeleton(ng_plan, skel, target_duration_sec=4.0)
    assert any("narration_jp" in e for e in errs), errs
    # 25字は許容内
    ok_plan = _plan_for(skel, "あ" * 25)
    errs = director._validate_plan_matches_skeleton(ok_plan, skel, target_duration_sec=4.0)
    assert not any("narration_jp" in e for e in errs), errs


def test_char_budget_uses_rhythm_cps_when_no_expected_chars():
    """expected 無しで rhythm cps がある場合、cps×尺 を budget とする。"""
    # cps=10, 尺=4.0 → budget 40, 許容 46 → 47字不合格
    skel = _minimal_skeleton(chars_per_sec=10.0, expected_chars=None)
    ng_plan = _plan_for(skel, "あ" * 60)
    errs = director._validate_plan_matches_skeleton(ng_plan, skel, target_duration_sec=4.0)
    assert any("narration_jp" in e for e in errs), errs
    ok_plan = _plan_for(skel, "あ" * 40)
    errs = director._validate_plan_matches_skeleton(ok_plan, skel, target_duration_sec=4.0)
    assert not any("narration_jp" in e for e in errs), errs


def test_char_budget_error_message_includes_hint():
    """不合格メッセージには「短く書き直せ」の修正指示ヒントを含めること（LLM矯正用）。"""
    skel = _minimal_skeleton(expected_chars=10)
    plan = _plan_for(skel, "あ" * 30)
    errs = director._validate_plan_matches_skeleton(plan, skel, target_duration_sec=4.0)
    matching = [e for e in errs if "narration_jp" in e]
    assert matching, errs
    joined = " / ".join(matching)
    assert "短く" in joined or "削" in joined or "冗長" in joined


def test_char_budget_empty_narration_is_ok():
    """narration_jp が空文字/None ならバジェット検査でエラーを出さない（別 validator の責務）。"""
    skel = _minimal_skeleton(expected_chars=15)
    plan = _plan_for(skel, "")
    errs = director._validate_plan_matches_skeleton(plan, skel, target_duration_sec=4.0)
    assert not any("narration_jp" in e for e in errs), errs


# ---------------------------------------------------------------------------
# TTS尺整合ガード（render.apply_tempo_guard）
# ---------------------------------------------------------------------------

def _which_ffmpeg():
    """テスト用の ffmpeg バイナリを決定する（bin/ffmpeg が無ければ system の ffmpeg）。"""
    repo_ffmpeg = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "ffmpeg"))
    if os.path.exists(repo_ffmpeg) and os.access(repo_ffmpeg, os.X_OK):
        return repo_ffmpeg
    which = shutil.which("ffmpeg")
    if which:
        return which
    return None


def _which_ffprobe():
    repo_ffprobe = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "ffprobe"))
    if os.path.exists(repo_ffprobe) and os.access(repo_ffprobe, os.X_OK):
        return repo_ffprobe
    which = shutil.which("ffprobe")
    if which:
        return which
    return None


def _make_silence(ffmpeg_bin, path, dur_sec):
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", "{:.3f}".format(dur_sec), "-c:a", "pcm_s16le", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def test_tempo_guard_noop_when_within_target():
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        _make_silence(ffmpeg, in_path, 3.0)
        res = render.apply_tempo_guard(
            ffmpeg, ffprobe, in_path, out_path, target_duration_sec=4.0,
            current_duration_sec=3.0, max_tempo=1.15,
        )
        assert res["applied"] is False
        assert res["tempo"] == 1.0


def test_tempo_guard_compresses_to_target_when_within_max_tempo():
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        _make_silence(ffmpeg, in_path, 3.3)  # 10% overrun over target 3.0 → tempo 1.10
        res = render.apply_tempo_guard(
            ffmpeg, ffprobe, in_path, out_path, target_duration_sec=3.0,
            current_duration_sec=3.3, max_tempo=1.15,
        )
        assert res["applied"] is True
        assert 1.05 <= res["tempo"] <= 1.15
        # 新尺は target 付近
        assert abs(res["new_duration_sec"] - 3.0) < 0.2


def test_tempo_guard_caps_at_max_tempo():
    """target 大幅超過なら tempo=max_tempo(1.15) で最大圧縮のみ（それでも target を超える）。"""
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        # 50% 超過 → 1.15 では吸収しきれない
        _make_silence(ffmpeg, in_path, 4.5)
        res = render.apply_tempo_guard(
            ffmpeg, ffprobe, in_path, out_path, target_duration_sec=3.0,
            current_duration_sec=4.5, max_tempo=1.15,
        )
        assert res["applied"] is True
        assert abs(res["tempo"] - 1.15) < 0.001
        # 新尺 ≈ 4.5 / 1.15 ≈ 3.91 秒（target=3.0 は超えたまま）
        assert res["new_duration_sec"] > 3.0


def test_tempo_guard_respects_max_tempo_lower_bound_1_0():
    """target が現在尺以上（余裕あり）なら圧縮しない=応用しない。"""
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        _make_silence(ffmpeg, in_path, 3.0)
        res = render.apply_tempo_guard(
            ffmpeg, ffprobe, in_path, out_path, target_duration_sec=3.0,
            current_duration_sec=3.0, max_tempo=1.15,
        )
        assert res["applied"] is False


def test_tempo_guard_clamps_invalid_max_tempo_below_1_0():
    """max_tempo < 1.0 が渡されても atempo=1.0 相当（=noop 相当）にクランプされる。

    ★意図: 呼び出し側の指定ミスで音声を「遅く」してしまうバグを防ぐ。
    """
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        _make_silence(ffmpeg, in_path, 3.5)  # overrun 相当
        res = render.apply_tempo_guard(
            ffmpeg, ffprobe, in_path, out_path, target_duration_sec=3.0,
            current_duration_sec=3.5, max_tempo=0.5,  # 不正
        )
        # max_tempo は 1.0 にクランプされ、cur/tgt=1.17 -> 1.0 -> 圧縮なし
        # ただし cur > tgt なので applied=True で tempo=1.0（=実質 noop、実尺 ≒ 3.5）を許容する。
        if res["applied"]:
            # atempo=1.0 適用のケース: 尺は元のまま
            assert abs(res["tempo"] - 1.0) < 0.001
            assert abs(res["new_duration_sec"] - 3.5) < 0.15
        else:
            # あるいはガードが「1.0 で圧縮不要」と判断して noop 返却でもOK
            assert res["tempo"] == 1.0


def test_tempo_guard_returns_noop_when_ffmpeg_binary_missing():
    """ffmpeg バイナリ不在（FileNotFoundError）は例外を投げず noop 返却する。"""
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        out_path = os.path.join(td, "out.wav")
        # in_path は存在しなくてもよい（ffmpeg が起動しないので）
        res = render.apply_tempo_guard(
            "/nonexistent/ffmpeg", "/nonexistent/ffprobe", in_path, out_path,
            target_duration_sec=3.0, current_duration_sec=3.5, max_tempo=1.15,
        )
        assert res["applied"] is False
        assert res["tempo"] == 1.0


# ---------------------------------------------------------------------------
# 共通ヘルパ: enforce_tempo_guard_on_segments
# ---------------------------------------------------------------------------

def test_enforce_tempo_guard_on_segments_uses_plan_minus_sync_gap():
    """複数 segment に一括適用。plan_dur - SYNC_GAP_SEC を目標に圧縮する。"""
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        s1 = os.path.join(td, "s1.wav")
        s2 = os.path.join(td, "s2.wav")
        _make_silence(ffmpeg, s1, 2.0)  # plan 3.0 → 余裕あり
        _make_silence(ffmpeg, s2, 4.0)  # plan 3.0 → 33% overrun
        segments = [{"path": s1, "duration_sec": 2.0}, {"path": s2, "duration_sec": 4.0}]
        adjusted, adjustments = render.enforce_tempo_guard_on_segments(
            segments, plan_durations=[3.0, 3.0], ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe,
            shot_ids=["a", "b"],
        )
        # 1個目は圧縮無し、2個目のみ圧縮
        assert len(adjusted) == 2
        assert adjusted[0]["path"] == s1  # 変更なし
        assert adjustments and len(adjustments) == 1
        adj = adjustments[0]
        assert adj["shot_id"] == "b"
        assert adj["tempo"] <= render.TEMPO_GUARD_MAX_TEMPO + 1e-6
        # 圧縮後の TTS 尺 + SYNC_GAP_SEC が plan + 許容内なら fully_absorbed
        # 実効目標 = 3.0 - 0.25 = 2.75, 元 4.0 -> tempo = min(4.0/2.75, 1.15) = 1.15
        # 新尺 = 4.0 / 1.15 ≒ 3.48。表示尺 = 3.48+0.25 = 3.73 > 3.3(plan*1.1)
        # → fully_absorbed=False
        assert adj["fully_absorbed"] is False


def test_enforce_tempo_guard_triggers_on_display_overrun_not_raw():
    """発火判定は「表示尺 = TTS + SYNC_GAP_SEC」ベース（TTS 単体の +10% 判定では見逃すケース）。

    plan=3.0, gap=0.25, overrun=10% → threshold 3.3. TTS=3.10 は素の判定では見逃されるが、
    表示尺 3.35 > 3.3 なので発火する。
    """
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        s1 = os.path.join(td, "s1.wav")
        _make_silence(ffmpeg, s1, 3.10)  # 素の +10% 判定では 3.30 に届かない
        adjusted, adjustments = render.enforce_tempo_guard_on_segments(
            [{"path": s1, "duration_sec": 3.10}],
            plan_durations=[3.0], ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe,
            shot_ids=["x"],
        )
        # 表示尺 3.35 > 3.3(=plan*1.10) なので発火する
        assert len(adjustments) == 1, "表示尺ベースの発火判定になっているべき"
        assert adjustments[0]["shot_id"] == "x"


def test_enforce_tempo_guard_on_segments_records_only_applied():
    """圧縮対象が無ければ adjustments は空リスト。"""
    ffmpeg = _which_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    ffprobe = _which_ffprobe()
    with tempfile.TemporaryDirectory() as td:
        s1 = os.path.join(td, "s1.wav")
        _make_silence(ffmpeg, s1, 2.0)
        adjusted, adjustments = render.enforce_tempo_guard_on_segments(
            [{"path": s1, "duration_sec": 2.0}],
            plan_durations=[3.0], ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe,
        )
        assert adjusted[0]["path"] == s1
        assert adjustments == []
