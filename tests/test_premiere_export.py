# -*- coding: utf-8 -*-
"""premiere/ パッケージ（profile / srt / export_xmeml）のテスト。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import xml.dom.minidom as minidom

import pytest

from premiere import export_xmeml, profile, srt


# ---------------------------------------------------------------------------
# premiere.profile
# ---------------------------------------------------------------------------

def test_load_profile_loads_ttp_reference_from_real_assets_dir():
    """assets/profiles/ttp_reference.json が実際にロードでき、必須キーを持つ。"""
    prof = profile.load_profile(name="ttp_reference")
    assert prof["version"] == 1
    assert prof["structure"]["total_sec"] == 18
    assert prof["emphasis"]["red_circle"] is True
    assert prof["audio"]["sfx"] is False


def test_load_profile_missing_file_raises(tmp_path):
    with pytest.raises(profile.ProfileValidationError):
        profile.load_profile(name="does_not_exist", profiles_dir=str(tmp_path))


def test_load_profile_missing_required_key_raises(tmp_path):
    (tmp_path / "broken.json").write_text(
        json.dumps({"version": 1, "structure": {}, "telop": {}, "emphasis": {}}),  # audioが無い
        encoding="utf-8",
    )
    with pytest.raises(profile.ProfileValidationError):
        profile.load_profile(name="broken", profiles_dir=str(tmp_path))


def test_load_profile_invalid_json_raises(tmp_path):
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(profile.ProfileValidationError):
        profile.load_profile(name="bad", profiles_dir=str(tmp_path))


def test_load_profile_manual_drive_shallow_merge_overrides(tmp_path):
    base = {
        "version": 1,
        "structure": {"total_sec": 18},
        "telop": {"font": "Noto Sans JP Black", "fill": "#FFFFFF"},
        "emphasis": {"red_circle": True},
        "audio": {"sfx": False},
    }
    (tmp_path / "sample.json").write_text(json.dumps(base), encoding="utf-8")
    manual = {"telop": {"font": "Custom Font"}}  # telopキー全体を置き換える(浅いマージ)
    (tmp_path / "manual_drive.json").write_text(json.dumps(manual), encoding="utf-8")

    merged = profile.load_profile(name="sample", profiles_dir=str(tmp_path))
    assert merged["telop"] == {"font": "Custom Font"}  # 浅いマージ=telop全体が置き換わる
    assert merged["structure"] == {"total_sec": 18}  # 他キーは無変更
    assert merged["audio"] == {"sfx": False}


def test_load_profile_empty_manual_drive_is_ignored(tmp_path):
    base = {
        "version": 1, "structure": {}, "telop": {}, "emphasis": {}, "audio": {},
    }
    (tmp_path / "sample.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "manual_drive.json").write_text("", encoding="utf-8")  # 空ファイル

    merged = profile.load_profile(name="sample", profiles_dir=str(tmp_path))
    assert merged == base


def test_load_profile_manual_drive_empty_object_is_ignored(tmp_path):
    base = {
        "version": 1, "structure": {"a": 1}, "telop": {}, "emphasis": {}, "audio": {},
    }
    (tmp_path / "sample.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "manual_drive.json").write_text("{}", encoding="utf-8")  # 空オブジェクト

    merged = profile.load_profile(name="sample", profiles_dir=str(tmp_path))
    assert merged == base


def test_manual_drive_example_file_is_never_loaded_by_default():
    """assets/profiles/manual_drive.json.example はプレースホルダで、実運用中は
    manual_drive.json自体が存在しないため ttp_reference.json がそのまま返る。"""
    prof = profile.load_profile(name="ttp_reference")
    assert prof["telop"]["font"] == "Noto Sans JP Black"  # .exampleの上書き値ではなくbaseの値


# ---------------------------------------------------------------------------
# premiere.srt
# ---------------------------------------------------------------------------

def _shot(sid, order, enabled, caption, trim_start, trim_end):
    return {
        "id": sid, "order": order, "enabled": enabled, "caption": caption,
        "clip_path": "projects/p1/clips/{}.mp4".format(sid),
        "source_duration": 10.0, "trim": {"start": trim_start, "end": trim_end},
    }


def test_build_srt_basic_two_entries_with_timecodes():
    plan = {"shots": [
        _shot("s1", 0, True, "最初のキャプション", 0.0, 3.0),
        _shot("s2", 1, True, "次のキャプション", 0.0, 2.0),
    ]}
    text = srt.build_srt(plan)
    assert "1\n00:00:00,000 --> 00:00:03,000\n最初のキャプション\n" in text
    assert "2\n00:00:03,000 --> 00:00:05,000\n次のキャプション\n" in text


def test_build_srt_respects_order_field_not_list_order():
    """shotsリストの並びではなく order フィールドの昇順でタイムラインを組み立てる。"""
    plan = {"shots": [
        _shot("s2", 1, True, "後のショット", 0.0, 2.0),
        _shot("s1", 0, True, "先のショット", 0.0, 3.0),
    ]}
    text = srt.build_srt(plan)
    first_block_start = text.index("1\n")
    second_block_start = text.index("2\n")
    assert "先のショット" in text[first_block_start:second_block_start]
    assert "00:00:00,000 --> 00:00:03,000" in text[first_block_start:second_block_start]


def test_build_srt_uses_trimmed_duration_not_source_duration():
    """trim.start/end後の実尺でタイミングを組む（source_durationは無視）。"""
    plan = {"shots": [_shot("s1", 0, True, "トリム済み", 1.0, 2.5)]}
    text = srt.build_srt(plan)
    assert "00:00:00,000 --> 00:00:01,500" in text


def test_build_srt_skips_disabled_shots():
    plan = {"shots": [
        _shot("s1", 0, True, "有効", 0.0, 2.0),
        _shot("s2", 1, False, "無効なので出ない", 0.0, 2.0),
        _shot("s3", 2, True, "これも有効", 0.0, 1.0),
    ]}
    text = srt.build_srt(plan)
    assert "無効なので出ない" not in text
    assert "有効" in text
    assert "これも有効" in text
    # 無効ショットの尺はタイムラインに計上されない(s3はs1の直後=2.0秒開始)
    assert "00:00:02,000 --> 00:00:03,000" in text


def test_build_srt_skips_empty_caption():
    plan = {"shots": [_shot("s1", 0, True, "   ", 0.0, 2.0)]}
    text = srt.build_srt(plan)
    assert text == ""


def test_build_srt_wraps_long_caption_to_two_lines():
    long_caption = "あ" * 20
    plan = {"shots": [_shot("s1", 0, True, long_caption, 0.0, 2.0)]}
    text = srt.build_srt(plan)
    body = text.split("\n")[2:4]
    assert len(body[0]) == 13
    assert body[0] + body[1] == long_caption


def test_build_srt_timecode_format_has_comma_millis():
    plan = {"shots": [_shot("s1", 0, True, "x", 0.0, 1.234)]}
    text = srt.build_srt(plan)
    # BUG修正後: タイミングは「フレーム丸め→フレーム基準で累積→秒へ戻す」を通すため、
    # 1.234秒は30fpsで round(1.234*30)=37フレーム(=1.2333...秒)に量子化される
    # （xmemlのV1クリップ配置と同じフレーム値で一致させるための意図した挙動）。
    assert "00:00:00,000 --> 00:00:01,233" in text


def test_build_srt_skips_shot_shorter_than_one_frame():
    """trim後の実尺が1フレーム未満(30fpsでround後0フレーム)のショットはSRTエントリを出力しない
    （ゼロ長ショットガード）。"""
    plan = {"shots": [
        _shot("s1", 0, True, "有効な尺", 0.0, 1.0),
        _shot("s2", 1, True, "ゼロ長ショット", 0.0, 0.01),  # 0.01*30=0.3 -> round=0フレーム
        _shot("s3", 2, True, "その次", 0.0, 1.0),
    ]}
    text = srt.build_srt(plan)
    assert "ゼロ長ショット" not in text
    assert "有効な尺" in text
    assert "その次" in text
    # s2は0フレームとしてタイムラインに計上されないため、s3はs1の直後(1.0秒開始)から始まる
    assert "00:00:01,000 --> 00:00:02,000" in text


def test_build_srt_and_xmeml_frame_alignment_match_for_trimmed_shots():
    """trim 0.517秒x4ショットで、SRTの開始秒とxmemlクリップの開始フレーム/fpsが
    全ショットで一致する(誤差<1ms)ことを確認する(タイミングずれ修正の回帰テスト)。"""
    fps = 30
    n_shots = 4
    plan = {
        "shots": [
            _shot("s{}".format(i + 1), i, True, "キャプション{}".format(i + 1), 0.0, 0.517)
            for i in range(n_shots)
        ],
        "bgm": None, "sfx": [],
    }
    srt_text = srt.build_srt(plan, fps=fps)
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=_ttp_profile(), fps=fps)

    # SRTから各エントリの開始秒を抽出する
    def _parse_srt_start_seconds(text):
        starts = []
        for block in text.strip().split("\n\n"):
            lines = block.split("\n")
            tc = lines[1].split(" --> ")[0]
            h, m, rest = tc.split(":")
            s, ms = rest.split(",")
            starts.append(int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0)
        return starts

    srt_starts = _parse_srt_start_seconds(srt_text)
    assert len(srt_starts) == n_shots

    # xmemlから各clipitem(<name>S0N</name>)直後の<start>フレームを抽出する
    xml_starts_sec = []
    for i in range(n_shots):
        display = "S{:02d}".format(i + 1)
        marker = "<name>{}</name>".format(display)
        idx = xml_text.index(marker)
        clip_block = xml_text[idx:xml_text.index("</clipitem>", idx)]
        start_frame = int(clip_block.split("<start>")[1].split("</start>")[0])
        xml_starts_sec.append(start_frame / float(fps))

    for i in range(n_shots):
        assert abs(srt_starts[i] - xml_starts_sec[i]) < 0.001, (
            "shot {} で SRT開始秒({})とXML開始フレーム/fps({})が1ms以上ずれています".format(
                i, srt_starts[i], xml_starts_sec[i]
            )
        )


def test_build_srt_fractional_fps_normalized_same_as_xmeml():
    """fpsに小数(例:29.97)を渡しても、srt.build_srtとexport_xmeml.build_xmemlの両方が
    fps_to_timebase_ntsc() 経由で同じ timebase(=30)+ntsc(=TRUE) に正規化するため、
    タイミングが食い違わない（Phase A の NTSC 対応後）。"""
    fractional_fps = 29.97
    plan = {"shots": [_shot("s1", 0, True, "テスト", 0.0, 1.0)]}
    srt_text = srt.build_srt(plan, fps=fractional_fps)
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=_ttp_profile(), fps=fractional_fps)

    # 双方とも timebase=30 に正規化され、1.0秒 -> round(1.0*30)=30フレーム -> 30/30=1.0秒 で一致
    assert "00:00:00,000 --> 00:00:01,000" in srt_text
    assert "<start>0</start>\n            <end>30</end>" in xml_text
    assert "<timebase>30</timebase>" in xml_text
    assert "<ntsc>TRUE</ntsc>" in xml_text


def test_build_xmeml_skips_shot_shorter_than_one_frame():
    """trim後の実尺が1フレーム未満のショットはV1トラックにclipitemを出力しない
    （ゼロ長ショットガード。srt側のゼロ長スキップと同じ契約）。"""
    plan = {
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "caption": "有効",
             "clip_path": "projects/p1/clips/s1.mp4", "source_duration": 1.0,
             "trim": {"start": 0.0, "end": 1.0}},
            {"id": "s2", "order": 1, "enabled": True, "caption": "ゼロ長",
             "clip_path": "projects/p1/clips/s2.mp4", "source_duration": 1.0,
             "trim": {"start": 0.0, "end": 0.01}},  # 30fpsでround後0フレーム
        ],
        "bgm": None, "sfx": [],
    }
    prof = {
        "version": 1, "structure": {}, "telop": {},
        "emphasis": {"red_circle": False}, "audio": {"sfx": False},
    }
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=prof, fps=30)
    assert xml_text.count("<clipitem") == 1
    assert "s2.mp4" not in xml_text
    assert "<duration>30</duration>" in xml_text  # 合計尺はs1の30フレームのみ


def test_build_srt_no_enabled_shots_returns_empty_string():
    plan = {"shots": [_shot("s1", 0, False, "無効のみ", 0.0, 2.0)]}
    assert srt.build_srt(plan) == ""


def test_build_srt_empty_plan_returns_empty_string():
    assert srt.build_srt({}) == ""
    assert srt.build_srt(None) == ""


# ---------------------------------------------------------------------------
# premiere.export_xmeml
# ---------------------------------------------------------------------------

def _plan_two_shots(project_id="p1"):
    return {
        "shots": [
            {"id": "s2", "order": 1, "enabled": True, "caption": "2番目",
             "clip_path": "projects/{}/clips/s2.mp4".format(project_id),
             "source_duration": 3.0, "trim": {"start": 0.5, "end": 2.5}},
            {"id": "s1", "order": 0, "enabled": True, "caption": "最初",
             "clip_path": "projects/{}/clips/s1.mp4".format(project_id),
             "source_duration": 4.0, "trim": {"start": 0.0, "end": 3.0}},
            {"id": "s3", "order": 2, "enabled": False, "caption": "無効",
             "clip_path": "projects/{}/clips/s3.mp4".format(project_id),
             "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}},
        ],
        "narration_text": "",
        "bgm": {"file": "upbeat.mp3", "gain_db": -14.0, "ducking": True},
        "sfx": [{"file": "pop.mp3", "at_sec": 1.0, "gain_db": -6.0}],
        "subtitle_style": {"font_size": 76, "accent_color": "#FFD84D", "position": "lower", "preset": "default"},
    }


def _ttp_profile():
    return profile.load_profile(name="ttp_reference")


def test_build_xmeml_is_well_formed_xml():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    minidom.parseString(xml_text)  # 例外が出なければ整形式


def test_build_xmeml_v1_track_has_only_enabled_shots_in_order():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    assert xml_text.count("<clipitem") >= 2
    # order=0のs1(=S01)がs2(=S02)より先(タイムライン上start=0)に出る
    idx_s1 = xml_text.index("<name>S01</name>")
    idx_s2 = xml_text.index("<name>S02</name>")
    assert idx_s1 < idx_s2
    assert "s3.mp4" not in xml_text  # disabledショットは含まれない


def test_build_xmeml_in_out_frames_reflect_trim_at_30fps():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile(), fps=30)
    # s1: trim 0.0-3.0 -> in=0 out=90, timeline start=0 end=90
    assert "<start>0</start>\n            <end>90</end>\n            <in>0</in>\n            <out>90</out>" in xml_text
    # s2: trim 0.5-2.5(duration 2.0s=60f) -> timeline start=90 end=150, in=15 out=75
    assert "<start>90</start>\n            <end>150</end>\n            <in>15</in>\n            <out>75</out>" in xml_text
    assert "<duration>150</duration>" in xml_text  # 合計尺=90+60=150フレーム


def test_build_xmeml_pathurl_percent_encodes_spaces_in_path(tmp_path):
    project_dir = tmp_path / "claude code" / "proj"
    project_dir.mkdir(parents=True)
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), str(project_dir), profile=_ttp_profile())
    assert "claude%20code" in xml_text
    assert "claude code" not in xml_text  # 生の空白がpathurlに残っていない


def test_build_xmeml_bgm_gain_reflected_as_level_value():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", bgm_path="assets/bgm/upbeat.mp3", profile=_ttp_profile())
    expected_level = 10.0 ** (-14.0 / 20.0)
    assert "<value>{:.6f}</value>".format(expected_level) in xml_text


def test_build_xmeml_bgm_gain_falls_back_to_profile_when_plan_gain_missing():
    plan = _plan_two_shots()
    plan["bgm"] = {"file": "upbeat.mp3", "ducking": True}  # gain_db無し
    prof = _ttp_profile()
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", bgm_path="assets/bgm/upbeat.mp3", profile=prof)
    expected_level = 10.0 ** (prof["audio"]["bgm_gain_db"] / 20.0)
    assert "<value>{:.6f}</value>".format(expected_level) in xml_text


def test_build_xmeml_sfx_omitted_when_profile_audio_sfx_false():
    prof = _ttp_profile()
    assert prof["audio"]["sfx"] is False
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=prof)
    assert "clipitem-a3" not in xml_text
    assert "pop.mp3" not in xml_text


def test_build_xmeml_sfx_included_when_profile_audio_sfx_true():
    prof = _ttp_profile()
    prof_sfx_on = dict(prof, audio=dict(prof["audio"], sfx=True))
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=prof_sfx_on)
    assert "clipitem-a3" in xml_text
    assert "pop.mp3" in xml_text
    assert "<start>30</start>" in xml_text  # at_sec=1.0 * 30fps = 30frame


def test_build_xmeml_red_circle_placed_as_disabled_clip_on_v3():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    v3_idx = xml_text.index("RC01 red_circle")
    enabled_idx = xml_text.index("<enabled>FALSE</enabled>")
    # <enabled>FALSE</enabled> がred_circleクリップの直後の近傍にある(同一clipitem内)
    assert v3_idx < enabled_idx < v3_idx + 600
    assert "red_circle.png" in xml_text


def test_build_xmeml_red_circle_omitted_when_profile_emphasis_false():
    prof = _ttp_profile()
    prof_no_circle = dict(prof, emphasis=dict(prof["emphasis"], red_circle=False))
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=prof_no_circle)
    assert "RC01" not in xml_text
    assert "red_circle" not in xml_text


def test_build_xmeml_no_narration_or_bgm_omits_a1_a2_clips():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    assert "clipitem-a1" not in xml_text
    assert "clipitem-a2" not in xml_text


def test_build_xmeml_narration_spans_full_timeline_duration():
    xml_text = export_xmeml.build_xmeml(
        _plan_two_shots(), "/tmp/proj", narration_path="narration.wav", profile=_ttp_profile()
    )
    assert "<name>NAR narration</name>" in xml_text
    narration_block = xml_text[xml_text.index("<name>NAR narration</name>"):]
    assert "<start>0</start>" in narration_block.split("</clipitem>")[0]
    assert "<end>150</end>" in narration_block.split("</clipitem>")[0]


def test_build_xmeml_is_deterministic_for_same_input():
    plan = _plan_two_shots()
    prof = _ttp_profile()
    xml1 = export_xmeml.build_xmeml(plan, "/tmp/proj", narration_path="n.wav", bgm_path="b.mp3", profile=prof, fps=30)
    xml2 = export_xmeml.build_xmeml(plan, "/tmp/proj", narration_path="n.wav", bgm_path="b.mp3", profile=prof, fps=30)
    assert xml1 == xml2


def test_build_xmeml_different_project_dir_changes_uuid_but_stays_deterministic():
    plan = _plan_two_shots()
    prof = _ttp_profile()
    xml_a = export_xmeml.build_xmeml(plan, "/tmp/proj_a", profile=prof)
    xml_b = export_xmeml.build_xmeml(plan, "/tmp/proj_b", profile=prof)
    assert xml_a != xml_b  # pathurlが異なるため全体も異なる

    def _uuid_of(xml_text):
        start = xml_text.index("<uuid>") + len("<uuid>")
        end = xml_text.index("</uuid>")
        return xml_text[start:end]

    assert _uuid_of(xml_a) != _uuid_of(xml_b)


def test_build_xmeml_sequence_has_expected_resolution_and_fps():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile(), fps=24)
    assert "<width>1080</width>" in xml_text
    assert "<height>1920</height>" in xml_text
    assert "<timebase>24</timebase>" in xml_text
    assert "<ntsc>FALSE</ntsc>" in xml_text


def test_build_xmeml_golden_small_plan():
    """小さなplan(ショット1本・narration/bgm無し・red_circleオフ)の完全xmeml文字列を固定比較する。"""
    plan = {
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "caption": "ゴールデン",
             "clip_path": "projects/g1/clips/s1.mp4", "source_duration": 2.0, "trim": {"start": 0.0, "end": 1.0}},
        ],
        "bgm": None,
        "sfx": [],
    }
    prof = {
        "version": 1, "structure": {}, "telop": {},
        "emphasis": {"red_circle": False}, "audio": {"sfx": False},
    }
    xml_text = export_xmeml.build_xmeml(plan, "/golden/proj", profile=prof, fps=30)

    expected = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE xmeml>\n"
        '<xmeml version="4">\n'
        '  <sequence id="sequence-{sequence_id_hash}">\n'
        "    <uuid>{uuid}</uuid>\n"
        "    <name>reel_sequence</name>\n"
        "    <rate>\n"
        "      <timebase>30</timebase>\n"
        "      <ntsc>FALSE</ntsc>\n"
        "    </rate>\n"
        "    <duration>30</duration>\n"
        "    <media>\n"
        "      <video>\n"
        "        <format>\n"
        "          <samplecharacteristics>\n"
        "            <rate>\n"
        "              <timebase>30</timebase>\n"
        "              <ntsc>FALSE</ntsc>\n"
        "            </rate>\n"
        "            <width>1080</width>\n"
        "            <height>1920</height>\n"
        "          </samplecharacteristics>\n"
        "        </format>\n"
        "        <track>\n"
        '          <clipitem id="clipitem-v1-{clip_id_hash}">\n'
        "            <name>S01</name>\n"
        "            <rate>\n"
        "              <timebase>30</timebase>\n"
        "              <ntsc>FALSE</ntsc>\n"
        "            </rate>\n"
        "            <start>0</start>\n"
        "            <end>30</end>\n"
        "            <in>0</in>\n"
        "            <out>30</out>\n"
        "            <labels>\n"
        "              <label2>Iris</label2>\n"
        "            </labels>\n"
        '            <file id="file-v1-{file_id_hash}">\n'
        "              <name>s1.mp4</name>\n"
        "              <pathurl>file://localhost/golden/proj/projects/g1/clips/s1.mp4</pathurl>\n"
        "              <rate>\n"
        "                <timebase>30</timebase>\n"
        "                <ntsc>FALSE</ntsc>\n"
        "              </rate>\n"
        "              <duration>30</duration>\n"
        "              <media>\n"
        "                <video>\n"
        "                  <samplecharacteristics>\n"
        "                    <width>1080</width>\n"
        "                    <height>1920</height>\n"
        "                  </samplecharacteristics>\n"
        "                </video>\n"
        "              </media>\n"
        "            </file>\n"
        "          </clipitem>\n"
        "        </track>\n"
        "        <track>\n"
        "        </track>\n"
        "        <track>\n"
        "        </track>\n"
        "      </video>\n"
        "      <audio>\n"
        "        <track>\n"
        "        </track>\n"
        "        <track>\n"
        "        </track>\n"
        "        <track>\n"
        "        </track>\n"
        "      </audio>\n"
        "    </media>\n"
        "    <marker>\n"
        "      <name>S01開始</name>\n"
        "      <comment>s1</comment>\n"
        "      <in>0</in>\n"
        "      <out>-1</out>\n"
        "    </marker>\n"
        "  </sequence>\n"
        "</xmeml>\n"
    )
    # id/uuidはhashベースで決定論的だが値そのものはハッシュ関数依存のため、実際に生成された
    # 値を抽出してテンプレートへ埋め込み、それ以外(構造・タイミング・パス)を厳密一致で比較する。
    uuid_value = xml_text.split("<uuid>")[1].split("</uuid>")[0]
    sequence_id_hash = xml_text.split('<sequence id="sequence-')[1].split('">')[0]
    clip_id_hash = xml_text.split('<clipitem id="clipitem-v1-')[1].split('">')[0]
    file_id_hash = xml_text.split('<file id="file-v1-')[1].split('">')[0]
    import re
    hash_shape = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    for value in (uuid_value, sequence_id_hash, clip_id_hash, file_id_hash):
        assert hash_shape.match(value), "決定論idの形式が想定外です: {!r}".format(value)
    expected_filled = expected.format(
        sequence_id_hash=sequence_id_hash, uuid=uuid_value,
        clip_id_hash=clip_id_hash, file_id_hash=file_id_hash,
    )
    assert xml_text == expected_filled


# ---------------------------------------------------------------------------
# Phase A: fps正規化 / 実在チェック / ラベル / マーカー / ネスト禁止
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fps_in,expected_tb,expected_ntsc", [
    (23.976, 24, "TRUE"),
    (23.98, 24, "TRUE"),   # ±0.01 の許容誤差内
    (24, 24, "FALSE"),
    (24.0, 24, "FALSE"),
    (29.97, 30, "TRUE"),
    (29.96, 30, "TRUE"),   # ±0.01 の許容誤差内
    (30, 30, "FALSE"),
    (30.0, 30, "FALSE"),
    (59.94, 60, "TRUE"),
    (60, 60, "FALSE"),
])
def test_fps_to_timebase_ntsc_covers_reference_table(fps_in, expected_tb, expected_ntsc):
    tb, ntsc = export_xmeml.fps_to_timebase_ntsc(fps_in)
    assert (tb, ntsc) == (expected_tb, expected_ntsc)


def test_fps_to_timebase_ntsc_unknown_falls_back_with_warning():
    import warnings as _w
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        tb, ntsc = export_xmeml.fps_to_timebase_ntsc(25)
        assert (tb, ntsc) == (25, "FALSE")
        assert any("unknown fps" in str(w.message) for w in caught)


def test_fps_to_timebase_ntsc_none_defaults_to_default_fps():
    assert export_xmeml.fps_to_timebase_ntsc(None) == (export_xmeml.DEFAULT_FPS, "FALSE")


def test_build_xmeml_ntsc_flag_applied_to_all_rates_for_ntsc_fps():
    """23.976fpsだと シーケンス・各clipitem・各file の全 <rate> で timebase=24 / ntsc=TRUE。"""
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile(), fps=23.976)
    assert "<timebase>23</timebase>" not in xml_text
    assert xml_text.count("<timebase>24</timebase>") >= 4  # sequence + format + clipitems + files
    assert xml_text.count("<ntsc>TRUE</ntsc>") >= 4
    assert "<ntsc>FALSE</ntsc>" not in xml_text


def test_build_xmeml_v1_clips_have_iris_label():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    s01_block = xml_text.split("<name>S01</name>")[1].split("</clipitem>")[0]
    assert "<labels>" in s01_block
    assert "<label2>Iris</label2>" in s01_block


def test_build_xmeml_narration_and_bgm_have_labels():
    xml_text = export_xmeml.build_xmeml(
        _plan_two_shots(), "/tmp/proj",
        narration_path="narration.wav", bgm_path="assets/bgm/upbeat.mp3",
        profile=_ttp_profile(),
    )
    nar_block = xml_text.split("<name>NAR narration</name>")[1].split("</clipitem>")[0]
    bgm_block = xml_text.split("<name>BGM bgm</name>")[1].split("</clipitem>")[0]
    assert "<label2>Forest</label2>" in nar_block
    assert "<label2>Lavender</label2>" in bgm_block


def test_build_xmeml_sfx_gets_family_name_and_caribbean_label():
    prof = _ttp_profile()
    prof_sfx_on = dict(prof, audio=dict(prof["audio"], sfx=True))
    plan = _plan_two_shots()
    plan["sfx"] = [{"file": "whoosh_gen_005.wav", "at_sec": 0.5, "gain_db": -6.0}]
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=prof_sfx_on)
    assert "<name>SE01 whoosh</name>" in xml_text
    se_block = xml_text.split("<name>SE01 whoosh</name>")[1].split("</clipitem>")[0]
    assert "<label2>Caribbean</label2>" in se_block


def test_build_xmeml_red_circle_has_rose_label():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    rc_block = xml_text.split("<name>RC01 red_circle</name>")[1].split("</clipitem>")[0]
    assert "<label2>Rose</label2>" in rc_block


def test_build_xmeml_emits_shot_boundary_markers():
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    # 各shotの開始マーカーが sequence 直下 </media> の後に並ぶ
    tail = xml_text.rsplit("</media>", 1)[1]
    assert "<marker>" in tail
    assert "<name>S01開始</name>" in tail
    assert "<name>S02開始</name>" in tail
    # コメントに shot_id が入る
    assert "<comment>s1</comment>" in tail
    assert "<comment>s2</comment>" in tail
    # S01は timeline start=0
    s01_marker = tail.split("<name>S01開始</name>")[1].split("</marker>")[0]
    assert "<in>0</in>" in s01_marker
    # S02は S01実尺(90フレーム)経過後
    s02_marker = tail.split("<name>S02開始</name>")[1].split("</marker>")[0]
    assert "<in>90</in>" in s02_marker


def test_build_xmeml_emits_hook_and_cta_markers_from_plan_ids():
    plan = _plan_two_shots()
    plan["hook_end_shot_id"] = "s1"
    plan["cta_start_shot_id"] = "s2"
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=_ttp_profile())
    tail = xml_text.rsplit("</media>", 1)[1]
    assert "<name>hook_end</name>" in tail
    assert "<name>cta_start</name>" in tail
    hook_marker = tail.split("<name>hook_end</name>")[1].split("</marker>")[0]
    cta_marker = tail.split("<name>cta_start</name>")[1].split("</marker>")[0]
    # hook終了 = s1(=0-90) の終了フレーム=90 / CTA開始 = s2の開始フレーム=90
    assert "<in>90</in>" in hook_marker
    assert "<in>90</in>" in cta_marker


def test_build_xmeml_emits_sfx_intent_markers_when_sfx_enabled():
    prof = _ttp_profile()
    prof_sfx_on = dict(prof, audio=dict(prof["audio"], sfx=True))
    plan = _plan_two_shots()
    plan["sfx"] = [{"file": "whoosh_gen_005.wav", "at_sec": 1.0}]
    xml_text = export_xmeml.build_xmeml(plan, "/tmp/proj", profile=prof_sfx_on)
    tail = xml_text.rsplit("</media>", 1)[1]
    assert "<name>SE01 whoosh</name>" in tail  # SEの意図マーカー
    se_marker = tail.split("<marker>\n      <name>SE01 whoosh</name>")[1].split("</marker>")[0]
    assert "sfx whoosh" in se_marker
    assert "whoosh_gen_005.wav" in se_marker
    assert "<in>30</in>" in se_marker  # at_sec=1.0 * 30fps


def test_build_xmeml_no_nested_sequence_elements():
    """FCP7 XMLはネスト sequence を許すが、Phase A では使わない前提を固定する。"""
    xml_text = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    assert xml_text.count("<sequence") == 1
    assert "</sequence>" in xml_text
    assert xml_text.count("</sequence>") == 1


def test_build_xmeml_return_warnings_reports_missing_media(tmp_path):
    plan = _plan_two_shots()
    result = export_xmeml.build_xmeml(
        plan, str(tmp_path),  # tmp_pathの下に素材は無いので全部欠品
        narration_path="narration.wav", bgm_path="assets/bgm/upbeat.mp3",
        profile=_ttp_profile(), return_warnings=True,
    )
    assert isinstance(result, dict)
    assert "xmeml" in result and "warnings" in result
    kinds = {(w["role"], w["name"]) for w in result["warnings"]}
    # V1のショット2本 + V3赤丸 + A1ナレ + A2 BGM が欠品として報告される
    assert ("V1", "s1.mp4") in kinds
    assert ("V1", "s2.mp4") in kinds
    assert ("V3", "red_circle.png") in kinds
    assert ("A1", "narration.wav") in kinds
    assert ("A2", "upbeat.mp3") in kinds


def test_build_xmeml_return_warnings_empty_when_all_files_exist(tmp_path):
    """全素材が実在するときは warnings が空。"""
    # 全部の素材ファイルをtmp_pathに作る
    (tmp_path / "projects" / "p1" / "clips").mkdir(parents=True)
    (tmp_path / "projects" / "p1" / "clips" / "s1.mp4").write_bytes(b"x")
    (tmp_path / "projects" / "p1" / "clips" / "s2.mp4").write_bytes(b"x")
    (tmp_path / "assets" / "overlays").mkdir(parents=True)
    (tmp_path / "assets" / "overlays" / "red_circle.png").write_bytes(b"x")
    (tmp_path / "narration.wav").write_bytes(b"x")
    (tmp_path / "assets" / "bgm").mkdir(parents=True)
    (tmp_path / "assets" / "bgm" / "upbeat.mp3").write_bytes(b"x")

    result = export_xmeml.build_xmeml(
        _plan_two_shots(), str(tmp_path),
        narration_path="narration.wav", bgm_path="assets/bgm/upbeat.mp3",
        profile=_ttp_profile(), return_warnings=True,
    )
    assert result["warnings"] == []


def test_build_xmeml_return_warnings_default_returns_str_for_back_compat():
    """既定 return_warnings=False は従来どおり str を返す（後方互換）。"""
    out = export_xmeml.build_xmeml(_plan_two_shots(), "/tmp/proj", profile=_ttp_profile())
    assert isinstance(out, str)
