# -*- coding: utf-8 -*-
"""qa/premiere_qa.py の静的QA検査の単体テスト。

実機Premiereは不要。build_package と build_xmeml が生成する現実の Premiere
パッケージ相当のフィクスチャを組み立てて、
  (a) well-formed 判定（xmllint と etree 両方）
  (b) xml vs timeline.json の要素数一致
  (c) shot/caption/sfx 時刻の同期一致
  (d) plan v2 parity（project.json 経由）
を再現する。壊れたパッケージ（改変版）で検査が「NG」を返すことも確認する。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qa import premiere_qa


# ---------------------------------------------------------------------------
# パッケージ生成ヘルパ（既存 build_package を叩かず、export_xmeml から直接組む）
# ---------------------------------------------------------------------------

def _make_plan_v2():
    """test_premiere_sync_parity と同種の 3ショット plan v2。"""
    return {
        "version": 2,
        "concept": "qa-fixture",
        "hook": "テストフック",
        "narration_script": "テストナレ",
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "duration_sec": 2.0,
             "prompt": "", "caption": "フックです", "caption_jp": "フックです",
             "clip_path": "projects/px/clips/s1.mp4",
             "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}},
            {"id": "s2", "order": 1, "enabled": True, "duration_sec": 3.0,
             "prompt": "", "caption": "本編Aだよ", "caption_jp": "本編Aだよ",
             "clip_path": "projects/px/clips/s2.mp4",
             "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0}},
            {"id": "s3", "order": 2, "enabled": True, "duration_sec": 2.5,
             "prompt": "", "caption": "CTAコメント", "caption_jp": "CTAコメント",
             "clip_path": "projects/px/clips/s3.mp4",
             "source_duration": 2.5, "trim": {"start": 0.0, "end": 2.5}},
        ],
        "bgm_mood": "upbeat",
        "bgm": {"file": "upbeat_01.mp3", "gain_db": -14.0, "ducking": True},
        "sfx": [],
        "sfx_plan": [
            {"t_anchor": {"type": "cut", "shot_id": "s2", "offset_sec": 0.0}, "family": "whoosh"},
        ],
        "hook_end_shot_id": "s1",
        "cta_start_shot_id": "s3",
    }


def _build_fixture_package(tmp_path, plan=None, with_bgm=True):
    """export_xmeml.build_xmeml で reel.xml と timeline.json を生成し、captions.srt も置く。

    実運用の build_package と完全に同じではないが、qa/premiere_qa が見る
    reel.xml/timeline.json/captions.srt の3点を揃えた最小フィクスチャ。
    """
    from premiere import export_xmeml, srt as srt_mod
    from pipeline import edit_profile, render

    plan = plan or _make_plan_v2()
    durs = [float(s["duration_sec"]) for s in plan["shots"]]
    ep = edit_profile.load_edit_profile(project_seed="qa-fixture")
    enh = render.compute_edit_enhancement_kwargs(durs, ep, project_seed="qa-fixture", plan=plan)

    package_dir = tmp_path / "pkg"
    package_dir.mkdir()

    result = export_xmeml.build_xmeml(
        plan, str(tmp_path),
        profile={"version": 1, "structure": {}, "telop": {},
                 "emphasis": {"red_circle": False},
                 "audio": {"sfx": True, "bgm_gain_db": -14}},
        bgm_path=("/tmp/bgm.mp3" if with_bgm else None),
        fps=30, shot_display_durations=durs,
        sfx_events=enh["sfx_extra"],
        bgm_curve=(enh["bgm_curve"] if with_bgm else None),
        emit_captions=True, return_timeline=True,
    )

    (package_dir / "reel.xml").write_text(result["xmeml"], encoding="utf-8")
    (package_dir / "timeline.json").write_text(
        json.dumps(result["timeline"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (package_dir / "captions.srt").write_text(srt_mod.build_srt(plan), encoding="utf-8")
    return package_dir


# ---------------------------------------------------------------------------
# (a) well-formed
# ---------------------------------------------------------------------------

def test_check_reel_xml_well_formed_ok_on_valid_fixture(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    result = premiere_qa.check_reel_xml_well_formed(pkg)
    assert result["ok"] is True
    assert result["errors"] == []


def test_check_reel_xml_well_formed_missing_file(tmp_path):
    pkg = tmp_path / "empty"
    pkg.mkdir()
    result = premiere_qa.check_reel_xml_well_formed(pkg)
    assert result["ok"] is False
    assert "reel.xmlが存在しません" in result["errors"][0]


def test_check_reel_xml_well_formed_broken_xml(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    (pkg / "reel.xml").write_text("<xmeml><sequence></xmeml>", encoding="utf-8")  # 閉じタグ不整合
    result = premiere_qa.check_reel_xml_well_formed(pkg)
    assert result["ok"] is False
    assert any("失敗" in e or "パース" in e for e in result["errors"])


def test_check_reel_xml_falls_back_to_etree_when_no_xmllint(tmp_path, monkeypatch):
    pkg = _build_fixture_package(tmp_path)
    monkeypatch.setattr(premiere_qa, "_find_xmllint", lambda: None)
    result = premiere_qa.check_reel_xml_well_formed(pkg)
    assert result["ok"] is True
    assert result["tool"] == "etree"
    assert any("xmllint 未検出" in n for n in result["notes"])


# ---------------------------------------------------------------------------
# (b) xml vs timeline 一致
# ---------------------------------------------------------------------------

def test_check_xml_timeline_consistency_ok(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    result = premiere_qa.check_xml_timeline_consistency(pkg)
    assert result["ok"] is True, result["errors"]
    measured = result["measured"]
    assert measured["captions"]["xml"] == measured["captions"]["timeline"]
    assert measured["sfx"]["xml"] == measured["sfx"]["timeline"]
    assert measured["bgm_keyframes"]["xml"] >= 2  # bgm_curve 指定 -> keyframes 出ている


def test_check_xml_timeline_detects_caption_count_mismatch(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    tl = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
    # timeline 側の captions を1つ削って不整合を作る
    tl["captions"] = tl["captions"][:-1]
    (pkg / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    result = premiere_qa.check_xml_timeline_consistency(pkg)
    assert result["ok"] is False
    assert any("V2 generatoritem 数と timeline.captions が不一致" in e for e in result["errors"])


def test_check_xml_timeline_detects_missing_timeline_json(tmp_path):
    pkg = tmp_path / "empty"
    pkg.mkdir()
    result = premiere_qa.check_xml_timeline_consistency(pkg)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# (c) 同期一致
# ---------------------------------------------------------------------------

def test_check_sync_within_one_frame_ok(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    result = premiere_qa.check_sync_within_one_frame(pkg)
    assert result["ok"] is True, result["errors"]
    assert result["measured"]["timebase"] == 30
    assert abs(result["measured"]["one_frame_sec"] - (1.0 / 30.0)) < 1e-9


def test_check_sync_detects_shot_cumulative_drift(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    tl = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
    # 2番目のshot start_sec を意図的に大きくずらす
    tl["shots"][1]["start_sec"] = tl["shots"][1]["start_sec"] + 1.0
    (pkg / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    result = premiere_qa.check_sync_within_one_frame(pkg)
    assert result["ok"] is False
    assert any("start_sec 不整合" in e for e in result["errors"])


def test_check_sync_detects_caption_out_of_bounds(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    tl = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
    tl["captions"][0]["in_sec"] = -5.0  # shot範囲外
    tl["captions"][0]["out_sec"] = -1.0
    (pkg / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    result = premiere_qa.check_sync_within_one_frame(pkg)
    assert result["ok"] is False
    assert any("shot範囲外" in e for e in result["errors"])


def test_check_sync_flat_bgm_curve_does_not_require_hook_cta_keyframes(tmp_path):
    """BUG-56 回帰: hook_gain==body_gain==cta_gain のとき、xmemlは境界にkeyframeを打たない。
    QA も「gain が変わるときのみ」±1F 一致を要求する契約とする。
    """
    pkg = _build_fixture_package(tmp_path)
    tl = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
    # bgm_curve を flat (全 gain 同一) に書き換え。dip_events は残す
    if tl.get("bgm_curve"):
        tl["bgm_curve"]["hook_gain_db"] = -12
        tl["bgm_curve"]["body_gain_db"] = -12
        tl["bgm_curve"]["cta_gain_db"] = -12
    (pkg / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    result = premiere_qa.check_sync_within_one_frame(pkg)
    # flat curve では hook_end/cta_start に keyframe が無いのが正しい挙動 -> エラーにしない
    assert result["ok"] is True, result["errors"]


def test_check_sync_without_bgm_curve_skips_keyframe_check(tmp_path):
    pkg = _build_fixture_package(tmp_path, with_bgm=False)
    result = premiere_qa.check_sync_within_one_frame(pkg)
    assert result["ok"] is True
    # bgm_curve 指定なし -> keyframe_sync は None（未実施）
    assert result["measured"]["bgm_keyframe_sync_ok"] is None


# ---------------------------------------------------------------------------
# (d) plan v2 parity
# ---------------------------------------------------------------------------

def test_check_plan_parity_skips_when_no_project_json(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    result = premiere_qa.check_plan_parity(pkg)
    assert result["ok"] is True
    assert result["skipped"] is True


def test_check_plan_parity_ok_when_project_json_present(tmp_path):
    # projects/<id>/premiere/<ts>/ 構造を作る
    projects_dir = tmp_path / "projects" / "p_test"
    (projects_dir / "premiere").mkdir(parents=True)
    plan = _make_plan_v2()
    (projects_dir / "project.json").write_text(
        json.dumps({"plan": plan}, ensure_ascii=False), encoding="utf-8",
    )
    pkg = _build_fixture_package(projects_dir / "premiere", plan=plan)
    # pkgの位置を projects/<id>/premiere/pkg にする（デフォルトで pkg サブディレクトリ）
    result = premiere_qa.check_plan_parity(pkg)
    assert result["ok"] is True
    assert result["skipped"] is False


# ---------------------------------------------------------------------------
# 統合: run_all + CLI
# ---------------------------------------------------------------------------

def test_run_all_returns_report_with_four_checks(tmp_path):
    pkg = _build_fixture_package(tmp_path)
    report = premiere_qa.run_all(pkg)
    assert "well_formed" in report["checks"]
    assert "xml_timeline_consistency" in report["checks"]
    assert "sync_within_one_frame" in report["checks"]
    assert "plan_parity" in report["checks"]
    assert report["overall_ok"] is True


def test_cli_returns_zero_on_valid_package(tmp_path, capsys):
    pkg = _build_fixture_package(tmp_path)
    rc = premiere_qa.main([str(pkg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "overall_ok: True" in out


def test_cli_returns_two_on_broken_package(tmp_path, capsys):
    pkg = _build_fixture_package(tmp_path)
    (pkg / "reel.xml").write_text("<xmeml>bogus", encoding="utf-8")
    rc = premiere_qa.main([str(pkg)])
    assert rc == 2


def test_cli_json_output(tmp_path, capsys):
    pkg = _build_fixture_package(tmp_path)
    rc = premiere_qa.main([str(pkg), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["overall_ok"] is True
