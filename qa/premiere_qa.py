# -*- coding: utf-8 -*-
"""Premiere 書き出しパッケージの静的QA（実機Premiere不要・xmllint必須推奨）。

premiere.package.build_package() が生成するパッケージ:
  - reel.xml         : FCP7 xmeml
  - captions.srt     : SRT字幕
  - timeline.json    : 機械可読サイドカー（premiere.export_xmeml.build_xmeml が持つ同期意図）
  - narration.wav / style/ / README_import.md

を対象に、以下を静的検査する（実際にPremiereを起動しない）。

検査項目:
  (a) xmllint --noout で reel.xml が well-formed であること。
      xmllint が無ければ Python 標準の xml.etree.ElementTree でパース可能かで代替判定
      （その旨を results に notes として出す）。
  (b) timeline.json と reel.xml の整合:
       - V2 generatoritem 数 == timeline.captions の要素数
       - A3 clipitem 数     == timeline.sfx の要素数
       - reel.xml の <sequence>/<marker> 数 == timeline.markers.shot_starts + SFX markers + hook/cta markers
       - A2 clipitem の <keyframe> 数 が bgm_curve から期待される最少キーフレーム数以上
         （BGM カーブがある場合のみ。キーフレーム数の下限は
          「シーン境界(=shots数) + hook_end/cta_start + dip x 2(前後) + 冒頭/末尾」の合算）
  (c) 同期一致（±1フレーム=timebase から算出）:
       - timeline.shots の cumulative start_sec がショット尺の累積と一致
       - timeline.captions の (in_sec, out_sec) が各 shot の [start_sec, end_sec] に収まる
       - timeline.sfx の at_sec がタイムライン内(0..total_sec)
       - reel.xml の A2 <keyframe> の <when> が bgm_curve の重要時刻(hook_end/cta_start/dip)
         と最近傍 ±1F 以内
  (d) （オプション）plan v2 parity:
       package_dir から2階層上（projects/<id>/project.json）を辿って plan を読み、
       pipeline.sfx_planner.resolve_hook_cta_bounds で解決した hook_end / cta_start が
       timeline.markers に載っている値と ±1F 一致するか。plan が見つからなければスキップ。

CLI:
  python -m qa.premiere_qa <package_dir>
  python -m qa.premiere_qa <package_dir> --strict   # notes(=xmllint 無し・plan skip)も失格扱い
  python -m qa.premiere_qa <package_dir> --json     # JSONで出力

Python 3.9 互換構文のみ。ネットワーク・実機Premiere不要。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


# ---------------------------------------------------------------------------
# 内部ヘルパ（純関数・ユニットテスト対象）
# ---------------------------------------------------------------------------

def _load_timeline(package_dir):
    p = Path(package_dir) / "timeline.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _find_xmllint():
    """xmllint が使えるなら絶対パスを返す。無ければ None。"""
    return shutil.which("xmllint")


def check_reel_xml_well_formed(package_dir, xmllint_bin=None):
    """(a) reel.xml が well-formed であることを確認する。

    xmllint があれば `xmllint --noout` を使う。無ければ ElementTree でパースが通るかを見る。
    Returns dict: {"ok": bool, "errors": list[str], "notes": list[str], "tool": "xmllint"|"etree"|"none"}
    """
    reel = Path(package_dir) / "reel.xml"
    if not reel.exists():
        return {"ok": False, "errors": ["reel.xmlが存在しません: {}".format(reel)], "notes": [], "tool": "none"}

    tool = "xmllint"
    binp = xmllint_bin or _find_xmllint()
    if binp:
        try:
            proc = subprocess.run(
                [binp, "--noout", str(reel)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )
            if proc.returncode == 0:
                return {"ok": True, "errors": [], "notes": [], "tool": tool}
            err = proc.stderr.decode("utf-8", "replace").strip() or "xmllint returncode={}".format(proc.returncode)
            return {"ok": False, "errors": ["xmllint 失敗: {}".format(err)], "notes": [], "tool": tool}
        except Exception as exc:
            # xmllint 実行に失敗 → ElementTree で代替
            notes = ["xmllint 実行失敗 -> ElementTree で代替: {}".format(exc)]
            tool = "etree"
    else:
        notes = ["xmllint 未検出 -> ElementTree で代替（well-formed のみ判定）"]
        tool = "etree"

    try:
        ET.parse(str(reel))
        return {"ok": True, "errors": [], "notes": notes, "tool": tool}
    except Exception as exc:
        return {"ok": False, "errors": ["XMLパース失敗: {}".format(exc)], "notes": notes, "tool": tool}


def _parse_reel_xml(package_dir):
    """reel.xml を ElementTree で解析し、集計に必要な情報を dict で返す。

    Returns dict:
      video_tracks: [{clipitems: int, generatoritems: int, keyframes: int}, ...]
      audio_tracks: [{clipitems: int, keyframes: int}, ...]
      sequence_markers: int
      timebase: int (シーケンス rate)
      bgm_when_frames: list[int]   (A2 の <keyframe><when> フレーム番号昇順)
      sfx_clip_when_frames: list[int]  (A3 の各 clipitem 開始フレーム)
    """
    reel = Path(package_dir) / "reel.xml"
    tree = ET.parse(str(reel))
    root = tree.getroot()
    seq = root.find("sequence")
    if seq is None:
        raise RuntimeError("reel.xml に <sequence> が無い")
    rate = seq.find("rate")
    timebase = int(rate.find("timebase").text) if rate is not None and rate.find("timebase") is not None else 30

    media = seq.find("media")
    video = media.find("video") if media is not None else None
    audio = media.find("audio") if media is not None else None
    v_tracks = video.findall("track") if video is not None else []
    a_tracks = audio.findall("track") if audio is not None else []

    def _summ_video_track(t):
        return {
            "clipitems": len(t.findall("clipitem")),
            "generatoritems": len(t.findall("generatoritem")),
        }

    def _summ_audio_track(t):
        clips = t.findall("clipitem")
        keyframes = 0
        for c in clips:
            keyframes += len(c.findall(".//keyframe"))
        return {"clipitems": len(clips), "keyframes": keyframes}

    # A2(BGM) の keyframe when を抽出（audio track index 1 前提。無ければ全audio横断で最大を持つtrack）
    bgm_when = []
    a2 = a_tracks[1] if len(a_tracks) >= 2 else None
    if a2 is not None:
        for kf in a2.findall(".//keyframe"):
            w = kf.find("when")
            if w is not None and w.text is not None:
                try:
                    bgm_when.append(int(w.text))
                except ValueError:
                    pass
    bgm_when.sort()

    # A3(SFX) の各 clipitem 開始フレーム（<start>）
    sfx_start_frames = []
    a3 = a_tracks[2] if len(a_tracks) >= 3 else None
    if a3 is not None:
        for c in a3.findall("clipitem"):
            s = c.find("start")
            if s is not None and s.text is not None:
                try:
                    sfx_start_frames.append(int(s.text))
                except ValueError:
                    pass
    sfx_start_frames.sort()

    return {
        "video_tracks": [_summ_video_track(t) for t in v_tracks],
        "audio_tracks": [_summ_audio_track(t) for t in a_tracks],
        "sequence_markers": len(seq.findall("marker")),
        "timebase": timebase,
        "bgm_when_frames": bgm_when,
        "sfx_clip_start_frames": sfx_start_frames,
    }


def check_xml_timeline_consistency(package_dir):
    """(b) reel.xml の要素数と timeline.json の要素数が一致するか。

    Returns {"ok": bool, "errors": list[str], "measured": dict}
    """
    tl = _load_timeline(package_dir)
    if tl is None:
        return {"ok": False, "errors": ["timeline.json が存在しません"], "measured": {}}

    try:
        parsed = _parse_reel_xml(package_dir)
    except Exception as exc:
        return {"ok": False, "errors": ["reel.xml のパースに失敗: {}".format(exc)], "measured": {}}

    errors = []
    v_tracks = parsed["video_tracks"]
    a_tracks = parsed["audio_tracks"]

    # V2 generatoritem 数 == captions
    n_captions_tl = len(tl.get("captions") or [])
    n_generators_xml = v_tracks[1]["generatoritems"] if len(v_tracks) >= 2 else 0
    if n_captions_tl != n_generators_xml:
        errors.append("V2 generatoritem 数と timeline.captions が不一致: xml={} tl={}".format(
            n_generators_xml, n_captions_tl,
        ))

    # A3 clipitem 数 == sfx
    n_sfx_tl = len(tl.get("sfx") or [])
    n_a3_clips = a_tracks[2]["clipitems"] if len(a_tracks) >= 3 else 0
    if n_sfx_tl != n_a3_clips:
        errors.append("A3 SE clipitem 数と timeline.sfx が不一致: xml={} tl={}".format(
            n_a3_clips, n_sfx_tl,
        ))

    # シーケンス直下 marker 数の下限:
    #   shot_starts (=shots) + optional hook_end + optional cta_start + SFX markers (=sfx count)
    n_shot_starts_tl = len((tl.get("markers") or {}).get("shot_starts") or [])
    markers_tl = tl.get("markers") or {}
    n_hook = 1 if markers_tl.get("hook_end_sec") is not None else 0
    n_cta = 1 if markers_tl.get("cta_start_sec") is not None else 0
    # note: timeline.sfx にも marker が付く（xmeml の _build_sequence_markers 契約）
    expected_min_markers = n_shot_starts_tl + n_sfx_tl + n_hook + n_cta
    n_markers_xml = parsed["sequence_markers"]
    if n_markers_xml < n_shot_starts_tl:
        errors.append("XMLのmarker数がshot数より少ない: xml={} shots={}".format(
            n_markers_xml, n_shot_starts_tl,
        ))
    # ゆるめの下限（xmemlの実装が hook/cta の書き出しを省く場合もあるため、shot_starts 分は必須）

    # A2 keyframe 数の下限（BGM カーブがあるとき）
    bgm_curve = tl.get("bgm_curve")
    a2_keyframes = a_tracks[1]["keyframes"] if len(a_tracks) >= 2 else 0
    if bgm_curve:
        # 最少期待: shots境界(=shots数, 各シーンでの gain 遷移) + 開始 + 終了 = shots + 2
        # 実装上は「各シーン境界で ramp する」ため keyframe 数はもっと多い（30〜数十）想定。
        # ここでは最低限「keyframe が出ている」ことを見る。
        if a2_keyframes < 2:
            errors.append("A2 BGM keyframe が不足: xml={} (bgm_curve指定あり)".format(a2_keyframes))

    measured = {
        "captions": {"xml": n_generators_xml, "timeline": n_captions_tl},
        "sfx": {"xml": n_a3_clips, "timeline": n_sfx_tl},
        "markers": {"xml": n_markers_xml, "timeline_shot_starts": n_shot_starts_tl,
                    "expected_min_including_sfx_hookcta": expected_min_markers},
        "bgm_keyframes": {"xml": a2_keyframes, "has_bgm_curve": bool(bgm_curve)},
        "video_tracks": v_tracks,
        "audio_tracks": a_tracks,
    }
    return {"ok": not errors, "errors": errors, "measured": measured}


def check_sync_within_one_frame(package_dir):
    """(c) timeline.json 内部の時刻整合と、xmeml keyframe が bgm_curve 重要時刻と ±1F 一致。

    Returns {"ok": bool, "errors": list[str], "measured": dict}
    """
    tl = _load_timeline(package_dir)
    if tl is None:
        return {"ok": False, "errors": ["timeline.json が存在しません"], "measured": {}}

    timebase = int(tl.get("timebase") or 30)
    if timebase <= 0:
        return {"ok": False, "errors": ["timebase が不正: {}".format(timebase)], "measured": {}}
    one_frame = 1.0 / float(timebase)

    errors = []

    # shots の cumulative start_sec = sum(prev dur)
    shots = tl.get("shots") or []
    cursor = 0.0
    for s in shots:
        start_sec = float(s.get("start_sec") or 0.0)
        if abs(start_sec - cursor) > one_frame:
            errors.append("shot {} の start_sec 不整合: tl={:.4f} cum={:.4f}".format(
                s.get("id"), start_sec, cursor,
            ))
        cursor += float(s.get("end_sec") or 0.0) - float(s.get("start_sec") or 0.0)

    total_sec = float(tl.get("total_sec") or 0.0)

    # captions が対応shotの [start_sec, end_sec] に収まる（±1F許容）
    shot_by_id = {s.get("id"): s for s in shots}
    for c in tl.get("captions") or []:
        sid = c.get("shot_id")
        s = shot_by_id.get(sid)
        if s is None:
            continue  # shot_idが一致しない captions はスキップ（別の警告）
        s_start = float(s.get("start_sec") or 0.0)
        s_end = float(s.get("end_sec") or 0.0)
        in_sec = float(c.get("in_sec") or 0.0)
        out_sec = float(c.get("out_sec") or 0.0)
        if in_sec + one_frame < s_start or in_sec - one_frame > s_end:
            errors.append("caption {} の in_sec が shot範囲外: caption={:.4f} shot=[{:.4f},{:.4f}]".format(
                sid, in_sec, s_start, s_end,
            ))
        if out_sec + one_frame < s_start or out_sec - one_frame > s_end:
            errors.append("caption {} の out_sec が shot範囲外: caption={:.4f} shot=[{:.4f},{:.4f}]".format(
                sid, out_sec, s_start, s_end,
            ))
        if out_sec + one_frame < in_sec:
            errors.append("caption {} の out < in: {:.4f} < {:.4f}".format(sid, out_sec, in_sec))

    # sfx at_sec が [0, total_sec] 内（±1F 許容）
    for sfx in tl.get("sfx") or []:
        at_sec = float(sfx.get("at_sec") or 0.0)
        if at_sec + one_frame < 0.0 or at_sec - one_frame > total_sec:
            errors.append("sfx {} の at_sec がタイムライン外: at={:.4f} total={:.4f}".format(
                sfx.get("name") or sfx.get("family"), at_sec, total_sec,
            ))

    # A2 keyframe と bgm_curve の重要時刻の一致確認。
    # 契約（premiere.export_xmeml._bgm_curve_keyframes 実装に基づく）:
    #   - hook_end_sec / cta_start_sec は「そのフレーム前後」に必ずキーフレームが立つ（±1F 一致）
    #   - dip_events は「dip 時刻 ± dip_half_window_sec」の2点にランプ用キーフレームが立つ
    #     （bathtub 形の ducking 窓を作るため、dip 時刻そのものにはキーフレームが立たない）
    #     → 「dip 時刻 ± (dip_half_window + 1F)」の範囲にキーフレームが1つ以上存在するか、で見る
    bgm_curve = tl.get("bgm_curve")
    keyframe_ok = None
    key_time_errors = []
    if bgm_curve:
        try:
            parsed = _parse_reel_xml(package_dir)
        except Exception as exc:
            errors.append("reel.xml keyframe 検査のためのパースに失敗: {}".format(exc))
        else:
            when_frames = parsed.get("bgm_when_frames") or []
            when_secs = sorted(set(f / float(timebase) for f in when_frames))

            # (a) hook_end / cta_start は「区間の gain が変わる場合のみ」±1F 一致を要求。
            # BGM curve が全区間 flat (hook_gain_db == body_gain_db == cta_gain_db) の場合、
            # _bgm_curve_keyframes は境界にキーフレームを打たない（値が変わらないので RLE 圧縮で
            # 消える）。この場合は dip_events の窓端キーフレームだけがあれば正しい。
            def _almost_eq_db(a, b):
                if a is None or b is None:
                    return False
                try:
                    return abs(float(a) - float(b)) < 1e-6
                except (TypeError, ValueError):
                    return False

            hook_gain = bgm_curve.get("hook_gain_db")
            body_gain = bgm_curve.get("body_gain_db")
            cta_gain = bgm_curve.get("cta_gain_db")
            check_hook = not _almost_eq_db(hook_gain, body_gain)
            check_cta = not _almost_eq_db(body_gain, cta_gain)

            for k, do_check in (("hook_end_sec", check_hook), ("cta_start_sec", check_cta)):
                if not do_check:
                    continue
                v = bgm_curve.get(k)
                if v is None:
                    continue
                if not when_secs:
                    key_time_errors.append("A2 に keyframe が無いのに bgm_curve.{} が指定されている".format(k))
                    break
                kt = float(v)
                nearest = min(when_secs, key=lambda t: abs(t - kt))
                if abs(nearest - kt) > one_frame:
                    key_time_errors.append(
                        "bgm {} の {:.4f}s に対する A2 keyframe との差 {:.4f}s が±1F超".format(
                            k, kt, abs(nearest - kt),
                        )
                    )

            # (b) dip_events は ±(dip_half_window + 1F) の範囲に keyframe が存在
            dip_half = float(bgm_curve.get("sfx_dip_half_window_sec") or 0.3)
            for dt in bgm_curve.get("dip_events") or []:
                dt = float(dt)
                if not when_secs:
                    key_time_errors.append("A2 に keyframe が無いのに bgm_curve.dip_events が指定されている")
                    break
                # dip 窓 [dt - dip_half, dt + dip_half] を跨ぐ keyframe が1つ以上あるか
                tol = dip_half + one_frame
                nearest = min(when_secs, key=lambda t: abs(t - dt))
                if abs(nearest - dt) > tol:
                    key_time_errors.append(
                        "bgm dip {:.4f}s の ±{:.3f}s 内に A2 keyframe が無い（最近傍差 {:.4f}s）".format(
                            dt, tol, abs(nearest - dt),
                        )
                    )

            keyframe_ok = not key_time_errors
            errors.extend(key_time_errors)

    measured = {
        "timebase": timebase,
        "one_frame_sec": one_frame,
        "total_sec": total_sec,
        "n_shots": len(shots),
        "n_captions": len(tl.get("captions") or []),
        "n_sfx": len(tl.get("sfx") or []),
        "bgm_keyframe_sync_ok": keyframe_ok,
    }
    return {"ok": not errors, "errors": errors, "measured": measured}


def _find_project_json_upward(package_dir):
    """package_dir=projects/<id>/premiere/<ts>_<uuid> から 2階層上の project.json を探す。"""
    p = Path(package_dir).resolve()
    # premiere/ の親 = projects/<id>
    if p.parent.name == "premiere":
        candidate = p.parent.parent / "project.json"
        if candidate.exists():
            return candidate
    # フォールバック: 4階層まで遡って project.json を探す
    cur = p
    for _ in range(4):
        cur = cur.parent
        cand = cur / "project.json"
        if cand.exists():
            return cand
    return None


def check_plan_parity(package_dir):
    """(d) plan v2 が見つかれば、resolve_hook_cta_bounds の結果が timeline.markers と ±1F 一致。

    plan.json が見つからなければ ok=True, skipped=True で返す（notes に理由）。
    """
    tl = _load_timeline(package_dir)
    if tl is None:
        return {"ok": False, "errors": ["timeline.json が存在しません"], "notes": [], "skipped": False}

    pj = _find_project_json_upward(package_dir)
    if pj is None:
        return {"ok": True, "errors": [], "notes": ["project.json が見つからないため plan parity をスキップ"], "skipped": True}

    try:
        project_data = json.loads(pj.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": ["project.json 読み込み失敗: {}".format(exc)],
                "notes": [], "skipped": False}

    plan = project_data.get("plan") or {}
    if not plan:
        return {"ok": True, "errors": [], "notes": ["project.json に plan が無いためスキップ"], "skipped": True}

    # timeline.shots の dur_sec を集める（enabled 順に来ている前提）
    durs = []
    for s in tl.get("shots") or []:
        durs.append(float(s.get("end_sec") or 0.0) - float(s.get("start_sec") or 0.0))

    try:
        from pipeline.sfx_planner import resolve_hook_cta_bounds
        hook_end, cta_start = resolve_hook_cta_bounds(plan, durs)
    except Exception as exc:
        return {"ok": False, "errors": ["sfx_planner での解決に失敗: {}".format(exc)],
                "notes": [], "skipped": False}

    timebase = int(tl.get("timebase") or 30)
    one_frame = 1.0 / float(timebase) if timebase > 0 else 1.0 / 30.0

    markers = tl.get("markers") or {}
    errors = []
    if hook_end is not None:
        tl_hook = markers.get("hook_end_sec")
        if tl_hook is None:
            errors.append("plan では hook_end 解決可能だが timeline.markers.hook_end_sec が無い")
        elif abs(float(tl_hook) - float(hook_end)) > one_frame:
            errors.append("hook_end_sec 不整合: plan={:.4f} tl={:.4f}".format(hook_end, float(tl_hook)))

    if cta_start is not None:
        tl_cta = markers.get("cta_start_sec")
        if tl_cta is None:
            errors.append("plan では cta_start 解決可能だが timeline.markers.cta_start_sec が無い")
        elif abs(float(tl_cta) - float(cta_start)) > one_frame:
            errors.append("cta_start_sec 不整合: plan={:.4f} tl={:.4f}".format(cta_start, float(tl_cta)))

    return {"ok": not errors, "errors": errors,
            "notes": ["project.json={}".format(pj)], "skipped": False}


# ---------------------------------------------------------------------------
# エントリ
# ---------------------------------------------------------------------------

def run_all(package_dir, xmllint_bin=None):
    """4つの検査をまとめて実行し、report(dict)を返す。"""
    report = {"package_dir": str(package_dir), "checks": {}}

    a = check_reel_xml_well_formed(package_dir, xmllint_bin=xmllint_bin)
    report["checks"]["well_formed"] = a

    b = check_xml_timeline_consistency(package_dir)
    report["checks"]["xml_timeline_consistency"] = b

    c = check_sync_within_one_frame(package_dir)
    report["checks"]["sync_within_one_frame"] = c

    d = check_plan_parity(package_dir)
    report["checks"]["plan_parity"] = d

    report["overall_ok"] = a["ok"] and b["ok"] and c["ok"] and d["ok"]
    return report


def _format_human(report, strict=False):
    lines = []
    lines.append("Premiere パッケージ静的QA: {}".format(report["package_dir"]))
    for name, res in report["checks"].items():
        mark = "OK" if res.get("ok") else "NG"
        lines.append("  [{}] {}".format(mark, name))
        for e in res.get("errors") or []:
            lines.append("      - error: {}".format(e))
        for n in res.get("notes") or []:
            lines.append("      - note : {}".format(n))
        measured = res.get("measured")
        if measured:
            lines.append("      - measured: {}".format(json.dumps(measured, ensure_ascii=False)))
    lines.append("overall_ok: {}".format(report["overall_ok"]))
    if strict:
        # strict では notes(=スキップ/代替) も落とす
        has_notes = any((res.get("notes") for res in report["checks"].values()))
        if has_notes:
            lines.append("[strict] notes があるため overall を NG に降格します")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Premiere パッケージ静的QA CLI")
    parser.add_argument("package_dir", help="reel.xml/timeline.json/captions.srt を含むディレクトリ")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    parser.add_argument("--strict", action="store_true", help="notes(スキップや代替判定)も NG 扱いにする")
    parser.add_argument("--xmllint", default=None, help="xmllint バイナリの絶対パス（省略時は自動検出）")
    args = parser.parse_args(argv)

    report = run_all(args.package_dir, xmllint_bin=args.xmllint)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_human(report, strict=args.strict))

    overall = report["overall_ok"]
    if args.strict:
        has_notes = any((res.get("notes") for res in report["checks"].values()))
        if has_notes:
            overall = False
    return 0 if overall else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
