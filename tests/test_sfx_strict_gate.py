# -*- coding: utf-8 -*-
"""R4 SFX厳格ゲート: 「TTPモードで参考にSEが無い→出力SE完全ゼロ」をコード/コマンド構築で機械検証。

「効果音は元動画が無ければ無し、有ればちゃんとTTP」というユーザー要件の回帰防止。
実 ffmpeg は実行しない（コマンド構築レベルの検証）。
"""
from pipeline import sfx_planner, render, edit_profile
from pipeline.config import load_config


def _plan(n=4, dur=3.0, sfx_plan=None):
    shots = [{"id": "s{}".format(i), "duration_sec": dur} for i in range(n)]
    return {"shots": shots, "sfx_plan": sfx_plan if sfx_plan is not None else []}


def _durations(n=4, dur=3.0):
    return [dur] * n


def _ep():
    return edit_profile.load_edit_profile(load_config(), project_seed="gate-test", bgm_mood="upbeat")


# --- reference_sfx_is_absent 純関数 ---

def test_absent_true_for_empty_sfx_events():
    assert sfx_planner.reference_sfx_is_absent({"sfx_events": []}) is True


def test_absent_true_for_all_other_kind():
    spec = {"sfx_events": [{"t": 1.0, "kind": "other", "confidence": 0.9}]}
    assert sfx_planner.reference_sfx_is_absent(spec) is True


def test_absent_false_for_salient_events():
    spec = {"sfx_events": [
        {"t": 1.0, "kind": "transition", "confidence": 0.9},
        {"t": 2.0, "kind": "impact", "confidence": 0.8},
    ]}
    assert sfx_planner.reference_sfx_is_absent(spec) is False


def test_absent_false_when_reference_none():
    # 参考無し（テーマのみ生成）はゲート対象外＝従来動作
    assert sfx_planner.reference_sfx_is_absent(None) is False


# --- render.compute_edit_enhancement_kwargs ゲート ---

def test_gate_zero_sfx_when_reference_se_absent():
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), _ep(), project_seed="g", plan=_plan(),
        reference_spec={"sfx_events": [], "duration_sec": 12.0},
    )
    assert kw["sfx_extra"] == []
    assert kw["first_shot_impact_sec"] is None
    assert (kw["main_dip_events"] or []) == []
    # BGM ダッキング窓（dip_events）も作らない
    assert "dip_events" not in (kw["bgm_curve"] or {})


def test_gate_zero_sfx_when_only_other_kind():
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), _ep(), project_seed="g", plan=_plan(),
        reference_spec={"sfx_events": [{"t": 1.0, "kind": "other", "confidence": 0.9}], "duration_sec": 12.0},
    )
    assert kw["sfx_extra"] == []
    assert kw["first_shot_impact_sec"] is None


def test_no_gate_backward_compat_when_reference_none():
    # 従来動作: 参考 None なら演出音（cut SFX / first_shot_impact）は従来どおり出る
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), _ep(), project_seed="g", plan=_plan(), reference_spec=None,
    )
    assert len(kw["sfx_extra"]) >= 1
    assert kw["first_shot_impact_sec"] is not None


def test_no_gate_when_reference_has_salient_se():
    # 参考にSE有り＝既存経路を維持（SEが出る）
    ref = {"sfx_events": [
        {"t": 1.0, "kind": "transition", "confidence": 0.9},
        {"t": 4.0, "kind": "impact", "confidence": 0.9},
    ], "duration_sec": 12.0}
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), _ep(), project_seed="g", plan=_plan(), reference_spec=ref,
    )
    assert len(kw["sfx_extra"]) >= 1


# --- ffmpeg コマンド構築レベル（KR3: SE入力が1つも無い） ---

def _count_input_paths(cmd, paths):
    """cmd 内で、各 path が直前に "-i" を伴って現れる回数を数える。"""
    count = 0
    for i, tok in enumerate(cmd):
        if tok == "-i" and i + 1 < len(cmd) and cmd[i + 1] in paths:
            count += 1
    return count


def _build_cmd(sfx):
    return render.build_final_cmd(
        "ffmpeg", "concat.mp4", "narration.wav", "out.mp4", "subs.ass", "fonts",
        bgm_path=None, out_duration=12.0, sfx=sfx,
    )


def test_command_has_zero_se_inputs_when_gated():
    ep = _ep()
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), ep, project_seed="g", plan=_plan(),
        reference_spec={"sfx_events": [], "duration_sec": 12.0},
    )
    cmd = _build_cmd(kw["sfx_extra"])
    # SE 入力ゼロ = 入力は concat + narration の 2 つだけ
    assert cmd.count("-i") == 2


def test_command_has_se_inputs_when_present():
    ep = _ep()
    kw = render.compute_edit_enhancement_kwargs(
        _durations(), ep, project_seed="g", plan=_plan(), reference_spec=None,
    )
    sfx = kw["sfx_extra"]
    assert len(sfx) >= 1
    cmd = _build_cmd(sfx)
    sfx_paths = {s["path"] for s in sfx}
    assert _count_input_paths(cmd, sfx_paths) == len(sfx)
    assert cmd.count("-i") == 2 + len(sfx)
