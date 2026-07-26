# -*- coding: utf-8 -*-
"""telop_coverage 指標 + caption 空 shot バックフィル（実ペア第2弾#3）の単体テスト。

診断: 参考テロップ出現率100%に対し生成は58%（8shot中3shotがcaption空）。対策として
fidelity に telop_coverage を追加し、director の post-plan バックフィルで caption 空 shot を
根絶する。
"""
from qa import fidelity
from pipeline import director


def _spec_with_telops(dur, telops):
    return {"duration_sec": dur, "telops": telops}


def test_telop_coverage_detects_empty_shots():
    spec = _spec_with_telops(8.0, [{"start": 0.0, "end": 8.0, "text": "T"}])
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "あり"},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": ""},
    ]}
    tc = fidelity._telop_coverage(spec, plan)
    assert tc["gen_shots_total"] == 2
    assert tc["gen_shots_with_caption"] == 1
    assert tc["empty_caption_shot_ids"] == ["s2"]
    assert tc["score"] == 0.5


def test_telop_coverage_full_when_all_captioned():
    spec = _spec_with_telops(8.0, [{"start": 0.0, "end": 8.0, "text": "T"}])
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "a"},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": "b"},
    ]}
    tc = fidelity._telop_coverage(spec, plan)
    assert tc["score"] == 1.0
    assert tc["empty_caption_shot_ids"] == []


def test_telop_coverage_no_ref_telops_is_full_score():
    spec = _spec_with_telops(6.0, [])
    plan = {"shots": [{"id": "s1", "duration_sec": 6.0, "caption_jp": ""}]}
    tc = fidelity._telop_coverage(spec, plan)
    assert tc["score"] == 1.0  # 参考にテロップが無ければ減点しない
    assert tc["ref_has_telops"] is False


def test_compute_fidelity_exposes_telop_coverage():
    spec = _spec_with_telops(8.0, [{"start": 0.0, "end": 8.0, "text": "T", "position": "bottom"}])
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "a", "caption_in_offset_sec": 0.0, "caption_out_offset_sec": 4.0},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": "", },
    ]}
    fid = fidelity.compute_fidelity(spec, plan)
    assert "telop_coverage" in fid["summary"]
    assert "telop_coverage" in fid["details"]
    assert fid["summary"]["telop_coverage"] == 0.5


# --- backfill ---------------------------------------------------------------

def _skeleton(shot_ids, durs, ref_ranges, on_screen=None):
    on_screen = on_screen or {}
    shots = []
    for sid, d in zip(shot_ids, durs):
        sh = {"id": sid, "duration_sec": d}
        if sid in on_screen:
            sh["reference_visual"] = {"on_screen_text": on_screen[sid]}
        shots.append(sh)
    return {"shots": shots, "shot_ref_ranges": ref_ranges}


def test_backfill_persist_continues_prev_caption():
    """参考テロップが shot 開始境界を跨ぐ shot は前 shot の caption を継続する（PERSIST）。"""
    # ref telop start=1.0, end=6.0 → s2 の開始境界 4.0 を跨ぐ
    reference = {"duration_sec": 8.0, "telops": [{"start": 1.0, "end": 6.0, "text": "ずっと表示"}]}
    skeleton = _skeleton(["s1", "s2"], [4.0, 4.0], [(0.0, 4.0), (4.0, 8.0)])
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "毛穴が消えた", "telop_style_hint": {"position": "bottom"}},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": ""},
    ]}
    director._ensure_plan_telop_coverage(plan, skeleton, reference)
    assert plan["shots"][1]["caption_jp"] == "毛穴が消えた"
    assert plan["shots"][1]["caption_backfilled"] is True
    assert plan["shots"][1]["caption_in_offset_sec"] == 0.0
    assert plan["shots"][1]["caption_out_offset_sec"] == 4.0
    # 前 shot のスタイルヒントを継承
    assert plan["shots"][1]["telop_style_hint"]["position"] == "bottom"


def test_backfill_uses_storyboard_then_narration():
    """テロップが跨らない shot は storyboard 文字 → narration 断片の順で埋める。"""
    reference = {"duration_sec": 8.0, "telops": [{"start": 0.0, "end": 2.0, "text": "冒頭のみ"}]}
    skeleton = _skeleton(
        ["s1", "s2", "s3"], [3.0, 3.0, 2.0],
        [(0.0, 3.0), (3.0, 6.0), (6.0, 8.0)],
        on_screen={"s2": "10%→100%"},
    )
    plan = {"shots": [
        {"id": "s1", "duration_sec": 3.0, "caption_jp": "はじめ"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "", "narration_jp": "ここは説明です。"},
        {"id": "s3", "duration_sec": 2.0, "caption_jp": "", "narration_jp": "最後に一言、どうぞ。"},
    ]}
    director._ensure_plan_telop_coverage(plan, skeleton, reference)
    # s2: storyboard 文字を採用
    assert plan["shots"][1]["caption_jp"] == "10%→100%"
    # s3: narration 断片（先頭の一文＝句点まで）
    assert plan["shots"][2]["caption_jp"] == "最後に一言、どうぞ"
    # 全 shot が caption を持つ
    tc = fidelity._telop_coverage(reference, plan)
    assert tc["score"] == 1.0


def test_backfill_persist_from_captions_array_text():
    """前 shot が caption_jp 空でも captions[] に文言があれば PERSIST 継続元になる（codex P2）。"""
    # 境界 4.0 を跨ぐ ref telop で PERSIST 発火
    reference = {"duration_sec": 8.0, "telops": [{"start": 1.0, "end": 6.0, "text": "継続テロップ"}]}
    skeleton = _skeleton(["s1", "s2"], [4.0, 4.0], [(0.0, 4.0), (4.0, 8.0)])
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "",
         "captions": [{"text": "毛穴レス", "telop_style_hint": {"position": "top"}}]},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": ""},
    ]}
    director._ensure_plan_telop_coverage(plan, skeleton, reference)
    assert plan["shots"][1]["caption_jp"] == "毛穴レス"
    assert plan["shots"][1]["telop_style_hint"]["position"] == "top"


def test_backfill_prefers_storyboard_when_telop_changes_at_boundary(monkeypatch):
    """境界で参考テロップが切り替わっている shot は PERSIST せず storyboard/narration へ落ちる（codex P1）。"""
    # ref telops: 前は 0.0-3.5 で終わっており s2 の 4.0 は跨がない → 前 caption を継続させない
    reference = {"duration_sec": 8.0, "telops": [
        {"start": 0.0, "end": 3.5, "text": "先の文言"},
        {"start": 4.5, "end": 7.5, "text": "後の文言"},
    ]}
    skeleton = _skeleton(["s1", "s2"], [4.0, 4.0], [(0.0, 4.0), (4.0, 8.0)],
                        on_screen={"s2": "境界のstoryboard文字"})
    plan = {"shots": [
        {"id": "s1", "duration_sec": 4.0, "caption_jp": "前の生成caption"},
        {"id": "s2", "duration_sec": 4.0, "caption_jp": ""},
    ]}
    director._ensure_plan_telop_coverage(plan, skeleton, reference)
    # PERSIST しないため storyboard 文字が採用される
    assert plan["shots"][1]["caption_jp"] == "境界のstoryboard文字"


def test_backfill_noop_when_all_captioned():
    reference = {"duration_sec": 4.0, "telops": [{"start": 0.0, "end": 4.0, "text": "x"}]}
    skeleton = _skeleton(["s1"], [4.0], [(0.0, 4.0)])
    plan = {"shots": [{"id": "s1", "duration_sec": 4.0, "caption_jp": "そのまま"}]}
    director._ensure_plan_telop_coverage(plan, skeleton, reference)
    assert plan["shots"][0]["caption_jp"] == "そのまま"
    assert "caption_backfilled" not in plan["shots"][0]
