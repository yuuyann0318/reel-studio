# -*- coding: utf-8 -*-
"""Phase B: 同期一致テスト（心臓部）。

同じ plan v2 から、
  (a) ffmpeg レンダ経路が使う SE at_sec / bgm_curve / caption 表示秒
  (b) premiere/export_xmeml が xmeml タイムラインに配置する SE / caption / bgm keyframe 秒
が **±1フレーム（30fps で ±0.034s）** で一致することを機械検証する。

「plan v2 の同期意図が Premiere タイムラインへ忠実に反映されている」ことを保証するのが
本テストの狙い。plan v2 の sfx_plan / caption_in_offset_sec / hook-cta / BGM カーブの
どれかの経路で xmeml と ffmpeg が食い違ったら、このテストが赤くなる（そのバグは
Phase C（Premiere実機）に持ち込まないための番人）。
"""
from __future__ import annotations

import pytest

from pipeline import edit_profile, render, sfx_planner, subtitles
from premiere import export_xmeml


ONE_FRAME_TOL_SEC = 1.0 / 30.0  # 30fps で ±1 フレーム


# ---------------------------------------------------------------------------
# 共通 plan v2 fixture
# ---------------------------------------------------------------------------

def _make_plan_v2():
    """3ショット構成の plan v2。sfx_plan / hook-cta / caption offset を全部含む。"""
    return {
        "version": 2,
        "meta": {"source": "phaseB-parity"},
        "concept": "test",
        "hook": "テストフック",
        "narration_script": "テストナレーション",
        "shots": [
            {
                "id": "s1", "order": 0, "enabled": True,
                "duration_sec": 2.0,
                "prompt": "", "caption": "フックです", "caption_jp": "フックです",
                "clip_path": "projects/px/clips/s1.mp4",
                "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0},
                "caption_in_offset_sec": 0.2,
                "caption_out_offset_sec": 1.8,
            },
            {
                "id": "s2", "order": 1, "enabled": True,
                "duration_sec": 3.0,
                "prompt": "", "caption": "本編Aだよ", "caption_jp": "本編Aだよ",
                "clip_path": "projects/px/clips/s2.mp4",
                "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0},
                "caption_in_offset_sec": 0.4,
                "caption_out_offset_sec": 2.8,
            },
            {
                "id": "s3", "order": 2, "enabled": True,
                "duration_sec": 2.5,
                "prompt": "", "caption": "CTAコメント", "caption_jp": "CTAコメント",
                "clip_path": "projects/px/clips/s3.mp4",
                "source_duration": 2.5, "trim": {"start": 0.0, "end": 2.5},
                "caption_in_offset_sec": 0.0,
                "caption_out_offset_sec": 2.4,
            },
        ],
        "bgm_mood": "upbeat",
        "bgm": {"file": "upbeat_01.mp3", "gain_db": -14.0, "ducking": True},
        "sfx": [],
        "sfx_plan": [
            {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
            {"t_anchor": {"type": "caption_in", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
            {"t_anchor": {"type": "cut", "shot_id": "s3", "offset_sec": 0.0}, "family": "whoosh"},
        ],
        "hook_end_shot_id": "s1",
        "cta_start_shot_id": "s3",
    }


def _durations(plan):
    return [float(s["duration_sec"]) for s in plan["shots"]]


# ---------------------------------------------------------------------------
# KR1: SE の at_sec が ffmpeg 側と xmeml 側で ±1フレーム一致する
# ---------------------------------------------------------------------------

def test_sfx_at_sec_matches_between_ffmpeg_and_xmeml_within_one_frame():
    plan = _make_plan_v2()
    durations = _durations(plan)
    ep = edit_profile.load_edit_profile(project_seed="seed-parity")

    # (a) ffmpeg レンダ経路が渡す sfx_extra（実際にfilter graphに乗る at_sec 集合）
    enh = render.compute_edit_enhancement_kwargs(
        durations, ep, project_seed="seed-parity", plan=plan,
    )
    ffmpeg_ats = sorted(float(s["at_sec"]) for s in enh["sfx_extra"] or [])

    # (b) xmeml が並べる SE クリップの開始秒
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity", profile={"version":1,"structure":{},"telop":{},
                                        "emphasis":{"red_circle": False},
                                        "audio":{"sfx": True}},
        fps=30, shot_display_durations=durations,
        sfx_events=enh["sfx_extra"], bgm_curve=enh["bgm_curve"],
        emit_captions=True, return_timeline=True,
    )
    timeline = result["timeline"]
    xmeml_ats = sorted(float(x["at_sec"]) for x in timeline["sfx"])

    assert len(ffmpeg_ats) == len(xmeml_ats), (
        "SE数が一致していない: ffmpeg={} xmeml={}".format(ffmpeg_ats, xmeml_ats)
    )
    for a, b in zip(ffmpeg_ats, xmeml_ats):
        assert abs(a - b) <= ONE_FRAME_TOL_SEC, "SE時刻の差が±1フレームを超えた: {} vs {}".format(a, b)


# ---------------------------------------------------------------------------
# KR1: caption の in/out 秒が xmeml と subtitles pipe と SRT で ±1フレーム一致
# ---------------------------------------------------------------------------

def test_caption_timings_match_between_xmeml_and_pipeline_within_one_frame():
    plan = _make_plan_v2()
    durations = _durations(plan)

    # (a) subtitles.build_telop_pieces_from_shots が組む caption 表示区間（ffmpeg字幕焼き込み）
    telop_shots = [
        {"id": s["id"], "duration_sec": s["duration_sec"], "caption_jp": s["caption_jp"],
         "caption_in_offset_sec": s.get("caption_in_offset_sec"),
         "caption_out_offset_sec": s.get("caption_out_offset_sec")}
        for s in plan["shots"]
    ]
    pieces = subtitles.build_telop_pieces_from_shots(telop_shots, hook_shot_id="s1")
    ffmpeg_captions = [(p["out_start"], p["out_end"]) for p in pieces]

    # (b) xmeml V2 generatoritem が持つ caption 表示区間
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": True}},
        fps=30, shot_display_durations=durations,
        emit_captions=True, return_timeline=True,
    )
    xmeml_captions = [(c["in_sec"], c["out_sec"]) for c in result["timeline"]["captions"]]

    assert len(ffmpeg_captions) == len(xmeml_captions)
    for (fa, fb), (xa, xb) in zip(ffmpeg_captions, xmeml_captions):
        assert abs(fa - xa) <= ONE_FRAME_TOL_SEC, "caption 開始秒が±1F超: ffmpeg={} xmeml={}".format(fa, xa)
        assert abs(fb - xb) <= ONE_FRAME_TOL_SEC, "caption 終了秒が±1F超: ffmpeg={} xmeml={}".format(fb, xb)


# ---------------------------------------------------------------------------
# KR1: BGM curve の hook_end / cta_start / dip 時刻が ±1F 一致
# ---------------------------------------------------------------------------

def test_bgm_curve_key_times_match_between_ffmpeg_and_xmeml_within_one_frame():
    plan = _make_plan_v2()
    durations = _durations(plan)
    ep = edit_profile.load_edit_profile(project_seed="seed-parity")

    enh = render.compute_edit_enhancement_kwargs(
        durations, ep, project_seed="seed-parity", plan=plan,
    )
    ffmpeg_curve = enh["bgm_curve"]
    assert ffmpeg_curve is not None

    # xmeml A2 の keyframe を取り出す（timeline.json はキーフレーム時刻を持たないので
    # 生成した XML を直接パースして when= を確認する）
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": True,"bgm_gain_db":-14}},
        bgm_path="/tmp/bgm.mp3", fps=30, shot_display_durations=durations,
        sfx_events=enh["sfx_extra"], bgm_curve=ffmpeg_curve,
        emit_captions=True, return_timeline=True,
    )
    xml = result["xmeml"]
    import re
    when_frames = [int(m) for m in re.findall(r"<keyframe>\s*<when>(\d+)</when>", xml)]
    when_secs = sorted(set(f / 30.0 for f in when_frames))

    # ffmpeg 側の重要時刻: hook_end_sec / cta_start_sec / dip_events
    key_times_ffmpeg = {float(ffmpeg_curve.get("hook_end_sec") or 0.0),
                        float(ffmpeg_curve.get("cta_start_sec") or 0.0)}
    for dt in ffmpeg_curve.get("dip_events") or []:
        key_times_ffmpeg.add(float(dt))

    # xmeml の keyframe 群のうち、各 ffmpeg 側キータイムに最も近いものが ±1F 以内
    for kt in key_times_ffmpeg:
        # 最近傍
        nearest = min(when_secs, key=lambda t: abs(t - kt))
        assert abs(nearest - kt) <= ONE_FRAME_TOL_SEC, (
            "BGM curve のキータイム {:.4f}s に対する xmeml keyframe との差が±1F超: nearest={:.4f}s".format(
                kt, nearest,
            )
        )


# ---------------------------------------------------------------------------
# KR2 補助: xmeml が V2 に generatoritem を出す + BGM に keyframe を出す + SE が実尺
# ---------------------------------------------------------------------------

def test_xmeml_v2_has_generatoritem_per_enabled_caption():
    plan = _make_plan_v2()
    durations = _durations(plan)
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": False}},
        fps=30, shot_display_durations=durations,
        emit_captions=True, return_timeline=True,
    )
    xml = result["xmeml"]
    gen_count = xml.count("<generatoritem ")
    assert gen_count == 3, "generatoritem 数がcaption数と不一致: {}".format(gen_count)
    # 各テロップテキストが xmeml に埋め込まれている
    assert "フックです" in xml
    assert "本編Aだよ" in xml
    assert "CTAコメント" in xml


def test_xmeml_sfx_uses_actual_probed_duration_when_ffprobe_available(monkeypatch, tmp_path):
    """sfx_events の path が実在すれば、ffprobe で取得した実尺が xmeml の
    clipitem/file の duration に反映される。実尺取得に失敗したら SFX_PLACEHOLDER_DURATION_SEC。
    """
    plan = _make_plan_v2()
    durations = _durations(plan)
    # ffprobe を stub 化して固定尺(0.5秒)を返させる
    monkeypatch.setattr(export_xmeml, "_probe_media_duration_sec",
                        lambda p, ffprobe_bin=None, timeout_sec=5: 0.5)
    fake_sfx = [{"path": str(tmp_path / "fake.wav"), "at_sec": 2.0, "gain_db": -6.0}]
    (tmp_path / "fake.wav").write_bytes(b"RIFF")
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": True}},
        fps=30, shot_display_durations=durations,
        sfx_events=fake_sfx, emit_captions=False, return_timeline=True,
    )
    xml = result["xmeml"]
    # 0.5s @ 30fps = 15 frames
    assert "<duration>15</duration>" in xml
    # SE クリップの end-start = 15
    import re
    m = re.search(r'<clipitem id="clipitem-a3-[^"]+">\s*<name>SE01[^<]*</name>[\s\S]*?<start>60</start>\s*<end>75</end>', xml)
    assert m, "SE clipitem の start/end が実尺で並んでいない"


def test_xmeml_sfx_falls_back_to_placeholder_when_ffprobe_fails(monkeypatch, tmp_path):
    plan = _make_plan_v2()
    durations = _durations(plan)
    monkeypatch.setattr(export_xmeml, "_probe_media_duration_sec",
                        lambda p, ffprobe_bin=None, timeout_sec=5: None)
    (tmp_path / "fake.wav").write_bytes(b"RIFF")
    fake_sfx = [{"path": str(tmp_path / "fake.wav"), "at_sec": 2.0, "gain_db": -6.0}]
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": True}},
        fps=30, shot_display_durations=durations,
        sfx_events=fake_sfx, emit_captions=False, return_warnings=True,
    )
    xml = result["xmeml"]
    warnings = result["warnings"]
    # フォールバック尺 1.0s @ 30fps = 30 frames
    assert "<duration>30</duration>" in xml
    kinds = [w["kind"] for w in warnings]
    assert "sfx_duration_fallback" in kinds


# ---------------------------------------------------------------------------
# KR2 補助: SRT と xmeml が同じ caption 表示秒を出す（±1F）
# ---------------------------------------------------------------------------

def test_srt_and_xmeml_caption_timings_align_within_one_frame():
    from premiere import srt as srt_mod
    plan = _make_plan_v2()
    durations = _durations(plan)

    # SRT は plan.shots[].trim を使う（enabled + order sort）→ 同じ実尺経路
    srt_text = srt_mod.build_srt(plan, fps=30)
    import re
    tc_re = r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
    entries = re.findall(r"(" + tc_re + r") --> (" + tc_re + r")", srt_text)
    def _tc_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
    srt_intervals = []
    for e in entries:
        # e = (full, h1, m1, s1, ms1, full2, h2, m2, s2, ms2)
        start = _tc_to_sec(e[1], e[2], e[3], e[4])
        end = _tc_to_sec(e[6], e[7], e[8], e[9])
        srt_intervals.append((start, end))

    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": False}},
        fps=30, shot_display_durations=durations,
        emit_captions=True, return_timeline=True,
    )
    xmeml_captions = [(c["in_sec"], c["out_sec"]) for c in result["timeline"]["captions"]]

    assert len(srt_intervals) == len(xmeml_captions)
    for (sa, sb), (xa, xb) in zip(srt_intervals, xmeml_captions):
        assert abs(sa - xa) <= ONE_FRAME_TOL_SEC, "SRT start と xmeml が±1F超: {} vs {}".format(sa, xa)
        assert abs(sb - xb) <= ONE_FRAME_TOL_SEC, "SRT end と xmeml が±1F超: {} vs {}".format(sb, xb)


# ---------------------------------------------------------------------------
# hook/cta マーカーの v2 化
# ---------------------------------------------------------------------------

def test_hook_and_cta_markers_use_v2_measured_bounds():
    plan = _make_plan_v2()
    durations = _durations(plan)
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": False}},
        fps=30, shot_display_durations=durations,
        emit_captions=False, return_timeline=True,
    )
    tl = result["timeline"]
    m = tl["markers"]
    # v2 経由: hook_end = s1 の終端 = 2.0s, cta_start = s3 の開始 = 5.0s
    hook_end_sec, cta_start_sec = sfx_planner.resolve_hook_cta_bounds(plan, durations)
    assert abs(m.get("hook_end_sec", 0.0) - hook_end_sec) <= ONE_FRAME_TOL_SEC
    assert abs(m.get("cta_start_sec", 0.0) - cta_start_sec) <= ONE_FRAME_TOL_SEC


# ---------------------------------------------------------------------------
# unknown_fps warning が warnings_out に載る
# ---------------------------------------------------------------------------

def test_bgm_keyframes_restore_immediately_after_dip_window():
    """dip 窓の終端 (dt + half_window) の直後で BGM level が元セクション値に「即」戻る
    ことを確認する（codex 指摘: 従来実装は dip 終端で dipped のままキーフレームを打ち、
    次キーフレームまで linear ramp する回帰を残していた）。
    """
    from premiere.export_xmeml import _bgm_curve_keyframes, _db_to_level
    curve = {
        "hook_end_sec": 100.0, "cta_start_sec": 100.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12,
        "dip_events": [1.5], "sfx_dip_half_window_sec": 0.3, "sfx_dip_gain_db": -4,
    }
    kf, init = _bgm_curve_keyframes(curve, base_level=1.0, timebase=30, total_duration_frames=90)
    # dt=1.5, half=0.3 → dip active on [1.2s, 1.8s] = frames [36, 54]. Frame 55 onward: restored.
    frame_to_lv = dict(kf)
    hook_lvl = _db_to_level(-10)
    dip_mult = _db_to_level(-4)
    dipped = hook_lvl * dip_mult
    # dip 窓内の代表フレーム 40 は dip 値
    # dip 直後（frame 55）は元の hook_lvl に戻っている
    # 最終keyframeに近いところをスキャンして「dipped→hook 復帰」があることを確認
    # dip 終端(t=1.8s → 半フレームep で frame≒54)の直後、hook_lvl に「即」戻る
    # キーフレームが最初に現れることを確認する（線形 ramp ではなく step 復帰）。
    restored_frame = None
    for f, lv in kf:
        if f >= 50 and abs(lv - hook_lvl) < 1e-6:
            restored_frame = f
            break
    assert restored_frame is not None and restored_frame <= 60, (
        "dip 終端直後で hook_lvl に復帰しないキーフレーム列: {}".format(kf)
    )
    # dip 中の代表フレーム 45 は dipped 値
    lv_45 = None
    for f, lv in kf:
        if f == 45:
            lv_45 = lv
    # 直接 45 のキーフレームが無い場合は、直前の hold で dipped が保持されているはず。
    # 最初に f<=45 かつその後 f>45 で値が変わっているかで判定する。
    lv_at_45 = None
    for i, (f, lv) in enumerate(kf):
        if f <= 45:
            lv_at_45 = lv
        else:
            break
    assert lv_at_45 is not None and abs(lv_at_45 - dipped) < 1e-6, (
        "frame 45 (dip 中央) で dipped 値が保持されていない: got {}, expected {}".format(lv_at_45, dipped)
    )


def test_bgm_keyframes_hook_body_cta_step_transitions_are_sharp():
    """hook→body→cta の境界が Linear ramp ではなく step で切り替わることを確認する。"""
    from premiere.export_xmeml import _bgm_curve_keyframes, _db_to_level
    curve = {
        "hook_end_sec": 2.0, "cta_start_sec": 5.0,
        "hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12,
    }
    kf, init = _bgm_curve_keyframes(curve, base_level=1.0, timebase=30, total_duration_frames=225)
    hook_lvl = _db_to_level(-10)
    body_lvl = _db_to_level(-14)
    cta_lvl = _db_to_level(-12)
    # 初期値は hook
    assert abs(init - hook_lvl) < 1e-6
    # 境界の前後1フレームでの値が step 変化していること
    def _val_at(f_target):
        prev = None
        for f, lv in kf:
            if f <= f_target:
                prev = lv
            else:
                break
        return prev
    # hook_end=2s=60F, cta_start=5s=150F
    # frame 59 は hook, 60 は body
    assert abs(_val_at(59) - hook_lvl) < 1e-6
    assert abs(_val_at(60) - body_lvl) < 1e-6, "hook->body transition should be sharp at 60F"
    # frame 149 は body, 150 は cta
    assert abs(_val_at(149) - body_lvl) < 1e-6
    assert abs(_val_at(150) - cta_lvl) < 1e-6, "body->cta transition should be sharp at 150F"


def test_sfx_negative_at_sec_clamped_to_zero_matching_ffmpeg():
    """負の at_sec (director offset の異常値) は 0 にクランプする（ffmpeg 経路と一致）。"""
    plan = _make_plan_v2()
    durations = _durations(plan)
    sfx = [{"path": "/tmp/x.wav", "at_sec": -1.5, "gain_db": -6.0}]
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": True}},
        fps=30, shot_display_durations=durations,
        sfx_events=sfx, emit_captions=False, return_timeline=True,
    )
    # timeline の at_frame=0 でクランプ
    assert result["timeline"]["sfx"][0]["at_frame"] == 0
    # xmeml の start も 0
    xml = result["xmeml"]
    import re
    m = re.search(r'<clipitem id="clipitem-a3-[^"]+">\s*<name>SE01[^<]*</name>[\s\S]*?<start>0</start>', xml)
    assert m, "負 at_sec が 0 にクランプされて start=0 になっていない"


def test_unknown_fps_emits_warning_kind():
    plan = _make_plan_v2()
    durations = _durations(plan)
    result = export_xmeml.build_xmeml(
        plan, "/tmp/parity",
        profile={"version":1,"structure":{},"telop":{},"emphasis":{"red_circle": False},
                 "audio":{"sfx": False}},
        fps=48.5,  # 未知 fps
        shot_display_durations=durations,
        emit_captions=False, return_warnings=True,
    )
    warnings = result["warnings"]
    kinds = [w["kind"] for w in warnings]
    assert "unknown_fps" in kinds
