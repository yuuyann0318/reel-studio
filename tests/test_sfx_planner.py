# -*- coding: utf-8 -*-
"""pipeline/sfx_planner.py: plan v2 sfx_plan の実時刻解決・family→ファイル選定・
実測尺への追従・render 側配線・字幕同期の純関数テスト。実 ffmpeg は使わない。

TTP v2 Phase 3 の中核。従来の compute_cut_sfx_events（機械配置）が plan v1 用
フォールバックに降格し、plan v2 では sfx_planner の解決結果が採用されることを
機械検証する。
"""
from pipeline import sfx_planner, render, subtitles, edit_profile


# ---------------------------------------------------------------------------
# 最小 plan v2 fixture
# ---------------------------------------------------------------------------

def _base_plan_v2(shots=None, sfx_plan=None, hook_end_shot_id=None, cta_start_shot_id=None):
    """テスト用の最小 plan v2 を組み立てる（validate_plan は通さない=直接値検証専用）。"""
    if shots is None:
        shots = [
            {"id": "s1", "duration_sec": 2.0, "caption_jp": "フック"},
            {"id": "s2", "duration_sec": 3.0, "caption_jp": "本編A"},
            {"id": "s3", "duration_sec": 2.5, "caption_jp": "CTA"},
        ]
    return {
        "version": 2,
        "meta": {"source": "smoke"},
        "concept": "test",
        "hook": "test",
        "narration_script": "test",
        "shots": shots,
        "bgm_mood": "upbeat",
        "sfx_plan": sfx_plan or [],
        "hook_end_shot_id": hook_end_shot_id,
        "cta_start_shot_id": cta_start_shot_id,
    }


# ---------------------------------------------------------------------------
# アンカー解決: cut / caption_in / shot_start
# ---------------------------------------------------------------------------

def test_resolve_sfx_events_cut_anchor_resolves_to_shot_boundary_plus_offset():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A"
    )
    # s2 の開始 = shot0+shot1 の累積 = 2.0
    assert len(specs) == 1
    assert abs(specs[0]["at_sec"] - 2.0) < 1e-6


def test_resolve_sfx_events_shot_start_anchor_resolves_to_shot_boundary_plus_offset():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "shot_start", "shot_id": "s3", "offset_sec": 0.4}, "family": "impact"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A"
    )
    # s3 の開始 = 5.0, offset 0.4 → 5.4
    assert len(specs) == 1
    assert abs(specs[0]["at_sec"] - 5.4) < 1e-6


def test_resolve_sfx_events_caption_in_anchor_uses_caption_offset_of_that_shot():
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "A"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "B", "caption_in_offset_sec": 0.5},
        {"id": "s3", "duration_sec": 2.5, "caption_jp": "C"},
    ]
    plan = _base_plan_v2(shots=shots, sfx_plan=[
        {"t_anchor": {"type": "caption_in", "shot_id": "s2", "offset_sec": 0.0}, "family": "riser"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A"
    )
    # s2 開始 2.0 + caption_in 0.5 + offset 0.0 = 2.5
    assert len(specs) == 1
    assert abs(specs[0]["at_sec"] - 2.5) < 1e-6


# ---------------------------------------------------------------------------
# 実測尺追従（このフェーズの核心）: shot_display_durations が変わったら SE も追従する
# ---------------------------------------------------------------------------

def test_resolve_sfx_events_follows_actual_shot_display_durations_not_nominal():
    """s2 が同期モードで 0.4s 伸びたとき、s3 以降のカットSEが 0.4s 遅れて解決される。"""
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
        {"t_anchor": {"type": "cut", "shot_id": "s3", "offset_sec": 0.0}, "family": "impact"},
    ])
    manifest = edit_profile.load_sfx_manifest()

    # 名目尺
    specs_nom = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A"
    )
    # 実測: s2 が 0.4s 伸びた
    specs_actual = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.4, 2.5], manifest=manifest, project_seed="seed-A"
    )

    # s2 開始は変わらない（2.0）が、s3 開始が 5.0 → 5.4 にずれる
    assert abs(specs_nom[0]["at_sec"] - 2.0) < 1e-6
    assert abs(specs_nom[1]["at_sec"] - 5.0) < 1e-6
    assert abs(specs_actual[0]["at_sec"] - 2.0) < 1e-6
    assert abs(specs_actual[1]["at_sec"] - 5.4) < 1e-6


# ---------------------------------------------------------------------------
# family→ファイル選定の決定論・gain_db 優先・連続同音回避
# ---------------------------------------------------------------------------

def test_resolve_sfx_events_family_pick_is_deterministic_same_seed_same_files():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "impact"},
        {"t_anchor": {"type": "cut", "shot_id": "s3", "offset_sec": 0.0}, "family": "riser"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    a = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A")
    b = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A")
    assert [s["path"] for s in a] == [s["path"] for s in b]


def test_resolve_sfx_events_family_pick_stays_within_family():
    """family=impact のイベントは manifest 上 family="impact" のファイルから選ばれる。"""
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "impact"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A")
    assert len(specs) == 1
    # 選ばれた path を manifest でルックアップ → family が要求と一致
    picked_file = specs[0]["path"].rsplit("/", 1)[-1]
    matched = [e for e in manifest if e["file"] == picked_file or e["file"].endswith("/" + picked_file)]
    assert matched, "picked path not found in manifest: {}".format(specs[0]["path"])
    assert matched[0]["family"] == "impact"


def test_resolve_sfx_events_event_gain_db_overrides_default_gain():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh",
         "gain_db": -6},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A",
        default_gain_db=-18,
    )
    assert len(specs) == 1
    assert specs[0]["gain_db"] == -6.0


def test_resolve_sfx_events_default_gain_db_applied_when_event_omits_gain():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A",
        default_gain_db=-18,
    )
    assert len(specs) == 1
    assert specs[0]["gain_db"] == -18.0


# ---------------------------------------------------------------------------
# min_interval 間引き
# ---------------------------------------------------------------------------

def test_resolve_sfx_events_min_interval_thins_close_events():
    """s2 開始 (2.0) と s2 開始+0.5 (2.5) は min_interval=1.5 未満のため後者が間引かれる。"""
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.5}, "family": "impact"},
        {"t_anchor": {"type": "cut", "shot_id": "s3", "offset_sec": 0.0}, "family": "riser"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A",
        min_interval_sec=1.5,
    )
    # 2.0 と 5.0 が残り、2.5 は間引かれる
    at_secs = [s["at_sec"] for s in specs]
    assert 2.0 in at_secs
    assert 5.0 in at_secs
    assert 2.5 not in at_secs


# ---------------------------------------------------------------------------
# resolve_hook_cta_bounds: 実測境界での hook_end / cta_start 解決
# ---------------------------------------------------------------------------

def test_resolve_hook_cta_bounds_uses_actual_durations_not_nominal():
    plan = _base_plan_v2(hook_end_shot_id="s1", cta_start_shot_id="s3")
    # s1 が 0.4s 伸びた実測尺
    hook_end, cta_start = sfx_planner.resolve_hook_cta_bounds(plan, [2.4, 3.0, 2.5])
    # hook_end = s1 の終端 = 2.4
    assert abs(hook_end - 2.4) < 1e-6
    # cta_start = s3 の開始 = 2.4 + 3.0 = 5.4
    assert abs(cta_start - 5.4) < 1e-6


def test_resolve_hook_cta_bounds_returns_none_when_ids_missing():
    plan = _base_plan_v2()  # hook_end_shot_id/cta_start_shot_id 未設定
    hook_end, cta_start = sfx_planner.resolve_hook_cta_bounds(plan, [2.0, 3.0, 2.5])
    assert hook_end is None
    assert cta_start is None


def test_resolve_hook_cta_bounds_unknown_id_returns_none():
    plan = _base_plan_v2(hook_end_shot_id="not_a_shot", cta_start_shot_id="s3")
    hook_end, cta_start = sfx_planner.resolve_hook_cta_bounds(plan, [2.0, 3.0, 2.5])
    assert hook_end is None
    assert abs(cta_start - 5.0) < 1e-6


# ---------------------------------------------------------------------------
# render 側配線: plan v2 → sfx_planner 経路 / plan v1 → 既存経路
# ---------------------------------------------------------------------------

def test_compute_edit_enhancement_kwargs_uses_sfx_planner_when_plan_has_sfx_plan():
    """plan v2 (sfx_plan あり) では sfx_planner の結果が sfx_extra として採用される。"""
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
        {"t_anchor": {"type": "cut", "shot_id": "s3", "offset_sec": 0.0}, "family": "impact"},
    ], hook_end_shot_id="s1", cta_start_shot_id="s3")
    ep = edit_profile.load_edit_profile()

    kwargs = render.compute_edit_enhancement_kwargs(
        [2.0, 3.0, 2.5], ep, project_seed="seed-A", plan=plan
    )
    at_secs = [s["at_sec"] for s in kwargs["sfx_extra"]]
    # 明示指定した sfx_plan の 2 イベント（2.0, 5.0）が採用されている
    assert 2.0 in at_secs
    assert 5.0 in at_secs
    # sfx_planner 経路: bgm_curve も plan の hook/cta id で解決
    assert abs(kwargs["bgm_curve"]["hook_end_sec"] - 2.0) < 1e-6  # s1 の終端
    assert abs(kwargs["bgm_curve"]["cta_start_sec"] - 5.0) < 1e-6  # s3 の開始


def test_compute_edit_enhancement_kwargs_falls_back_to_legacy_when_plan_v1_or_none():
    """plan=None（v1相当）は既存経路（compute_cut_sfx_events + boundaries[1]/-1）を使う。"""
    ep = edit_profile.load_edit_profile()
    # plan 未指定 = 既存経路
    kwargs_a = render.compute_edit_enhancement_kwargs([2.0, 3.0, 2.5], ep, project_seed="seed-A")
    # plan v1 相当（sfx_plan 無し）= 既存経路
    kwargs_b = render.compute_edit_enhancement_kwargs(
        [2.0, 3.0, 2.5], ep, project_seed="seed-A",
        plan={"version": 1, "shots": []},
    )
    # 既存の bgm_curve は boundaries[1]=2.0, boundaries[-1]=5.0
    assert kwargs_a["bgm_curve"]["hook_end_sec"] == 2.0
    assert kwargs_a["bgm_curve"]["cta_start_sec"] == 5.0
    assert kwargs_b["bgm_curve"]["hook_end_sec"] == 2.0
    assert kwargs_b["bgm_curve"]["cta_start_sec"] == 5.0


def test_build_final_cmd_receives_plan_v2_sfx_events_as_input_paths():
    """plan v2 経路で選ばれた SFX パスが build_final_cmd の -i として並ぶ。"""
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    ep = edit_profile.load_edit_profile()
    kwargs = render.compute_edit_enhancement_kwargs(
        [2.0, 3.0, 2.5], ep, project_seed="seed-A", plan=plan
    )
    assert kwargs["sfx_extra"]
    picked_path = kwargs["sfx_extra"][0]["path"]

    cmd = render.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0, sfx=kwargs["sfx_extra"],
    )
    assert picked_path in cmd  # 実 SFX パスが -i として並んでいる
    joined = " ".join(cmd)
    # 2.0秒位置に adelay=2000|2000
    assert "adelay=2000|2000" in joined


# ---------------------------------------------------------------------------
# 統合: caption_in アンカーの SE 時刻と字幕 ASS の表示開始が ±0.1s 以内で一致
# ---------------------------------------------------------------------------

def _extract_ass_dialogue_start_secs(ass_text):
    """ASS 全文の Dialogue 行から Start 秒（フロート）だけ列挙する（テストヘルパ）。"""
    results = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        # "Dialogue: 0,H:MM:SS.cs,H:MM:SS.cs,Style,..."
        parts = line.split(",", 4)
        if len(parts) < 4:
            continue
        start_str = parts[1].strip()
        h, m, rest = start_str.split(":")
        sec = float(h) * 3600 + float(m) * 60 + float(rest)
        results.append(sec)
    return results


def test_caption_in_anchored_sfx_matches_subtitle_ass_display_start_within_0_1sec():
    """caption_in アンカーの SE 時刻と、そのショットのテロップ表示開始が ±0.1s 以内で一致する
    ＝ SE と字幕が同じ時刻源 caption_in_offset_sec を共有しているという核心の担保。"""
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "フックA"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "本編B", "caption_in_offset_sec": 0.5},
        {"id": "s3", "duration_sec": 2.5, "caption_jp": "CTA C"},
    ]
    plan = _base_plan_v2(shots=shots, sfx_plan=[
        {"t_anchor": {"type": "caption_in", "shot_id": "s2", "offset_sec": 0.0}, "family": "impact"},
    ])
    manifest = edit_profile.load_sfx_manifest()

    # SE 時刻
    specs = sfx_planner.resolve_sfx_events(
        plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A"
    )
    assert len(specs) == 1
    sfx_at = specs[0]["at_sec"]

    # 字幕 ASS の s2 テロップ表示開始時刻
    pieces = subtitles.build_telop_pieces_from_shots(shots, hook_shot_id="s1")
    # s2 の piece を取り出す（out_start は caption_in 反映済）
    s2_pieces = [p for p in pieces if "本編B" in p["lines"][0]]
    assert s2_pieces
    subtitle_start = s2_pieces[0]["out_start"]

    # ±0.1s 以内で一致（現状は完全一致で 2.5 == 2.5）
    assert abs(sfx_at - subtitle_start) < 0.1


def test_build_telop_pieces_from_shots_backward_compatible_when_no_offsets():
    """caption_in/out_offset_sec を持たないショットは従来どおりショット全区間で表示される。"""
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "A"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "B"},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert pieces[0]["out_start"] == 0.0
    assert pieces[0]["out_end"] == 2.0
    assert pieces[1]["out_start"] == 2.0
    assert pieces[1]["out_end"] == 5.0


def test_build_telop_pieces_from_shots_uses_caption_offsets_when_present():
    shots = [
        {"id": "s1", "duration_sec": 2.0, "caption_jp": "A"},
        {"id": "s2", "duration_sec": 3.0, "caption_jp": "B",
         "caption_in_offset_sec": 0.5, "caption_out_offset_sec": 2.5},
    ]
    pieces = subtitles.build_telop_pieces_from_shots(shots)
    assert pieces[1]["out_start"] == 2.5  # s2 開始 2.0 + 0.5
    assert pieces[1]["out_end"] == 4.5    # s2 開始 2.0 + 2.5


# ---------------------------------------------------------------------------
# 安全側: 空 sfx_plan / manifest 空 / 不明 shot_id
# ---------------------------------------------------------------------------

def test_resolve_sfx_events_empty_sfx_plan_returns_empty_list():
    plan = _base_plan_v2(sfx_plan=[])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=manifest)
    assert specs == []


def test_resolve_sfx_events_empty_manifest_yields_empty_specs():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
    ])
    specs = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=[])
    # manifest 無し → ファイル解決できないためスキップ
    assert specs == []


def test_resolve_sfx_events_unknown_shot_id_is_skipped():
    plan = _base_plan_v2(sfx_plan=[
        {"t_anchor": {"type": "cut", "shot_id": "s_unknown", "offset_sec": 0.0}, "family": "whoosh"},
        {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "impact"},
    ])
    manifest = edit_profile.load_sfx_manifest()
    specs = sfx_planner.resolve_sfx_events(plan, [2.0, 3.0, 2.5], manifest=manifest, project_seed="seed-A")
    # 未知 shot_id はスキップ、s2 の 1 件だけ残る
    assert len(specs) == 1
    assert abs(specs[0]["at_sec"] - 2.0) < 1e-6
