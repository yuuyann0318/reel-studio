#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""higgsfield-auto-reel メインオーケストレーター。

使い方:
    python run.py --theme "AIで副業を始める最初の一歩" --duration 30 --backend mock
    python run.py --theme "..." --duration 20 --backend mock --no-llm

段ごとに例外を握って output/report.json に記録し、途中失敗でも部分成果物を
work/<run_id>/ に残す（video-auto-editor の「解析まで完了・以降は失敗を記録」
という設計思想を踏襲）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from pipeline.config import load_config, project_root
from pipeline import director
from pipeline import compliance
from pipeline import subtitles
from pipeline import render
from pipeline import tts as tts_mod
from pipeline.visual import get_backend
from qa import qa_check

_ASSETS_DIR = project_root() / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"
_BGM_MANIFEST = _ASSETS_DIR / "bgm" / "manifest.json"
_FFMPEG_TIMEOUT_SEC = 1800

_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def _slugify(theme, max_len=24):
    s = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー]+", "-", theme or "reel").strip("-")
    return (s[:max_len] or "reel")


def _new_run_dir(theme):
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + "-" + _slugify(theme)
    run_dir = project_root() / "work" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def _resolve_bgm(mood):
    if not mood or mood == "none":
        return None
    if not _BGM_MANIFEST.exists():
        return None
    try:
        manifest = json.loads(_BGM_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return None
    for entry in manifest or []:
        if entry.get("mood") == mood:
            p = _ASSETS_DIR / "bgm" / entry.get("file", "")
            if p.exists():
                return str(p)
    return None


def _parse_loudnorm_json(stderr_text):
    m = _LOUDNORM_JSON_RE.search(stderr_text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return {
            "measured_I": data["input_i"],
            "measured_TP": data["input_tp"],
            "measured_LRA": data["input_lra"],
            "measured_thresh": data["input_thresh"],
            "offset": data.get("target_offset", "0.0"),
        }
    except Exception:
        return None


class StageError(RuntimeError):
    pass


def resolve_ng_words(cfg):
    """config の brand_rules.ng_words を「デフォルトNGワードへの追加分」として解決する。

    config未設定・ng_words未設定・ng_words=[] のいずれでも、compliance.DEFAULT_NG_WORDS
    （景表法NG表現・競合名義等）は常に有効にする。config側の値は追加分としてマージする。
    """
    config_ng_words = (cfg.get("brand_rules") or {}).get("ng_words") or []
    ng_words = list(compliance.DEFAULT_NG_WORDS)
    for w in config_ng_words:
        if w not in ng_words:
            ng_words.append(w)
    return ng_words


def _timed_stage(report, name):
    """`with _timed_stage(report, "director"): ...` で経過時間とok/errorをreportへ記録するcontext manager。"""

    class _Ctx:
        def __enter__(self_inner):
            self_inner.t0 = time.time()
            report["stages"].setdefault(name, {})
            return self_inner

        def __exit__(self_inner, exc_type, exc, tb):
            elapsed = time.time() - self_inner.t0
            entry = report["stages"].setdefault(name, {})
            entry["elapsed_sec"] = round(elapsed, 3)
            if exc is not None:
                entry["ok"] = False
                entry["error"] = str(exc)[:2000]
                return False  # 例外を再送出（呼び出し側run()で捕捉して停止判断）
            entry.setdefault("ok", True)
            return False

    return _Ctx()


def run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg):
    report = {
        "theme": theme,
        "target_duration_sec": target_duration_sec,
        "backend": backend_name,
        "no_llm": no_llm,
        "stages": {},
        "output_path": None,
        "qa": None,
        "ok": False,
    }

    run_id, run_dir = _new_run_dir(theme)
    report["run_id"] = run_id
    report["run_dir"] = str(run_dir)

    # --- Stage 1: director（企画生成） ---
    plan = None
    try:
        with _timed_stage(report, "director"):
            plan = director.run_director(theme, cfg, target_duration_sec=target_duration_sec, no_llm=no_llm)
            (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            report["stages"]["director"]["source"] = plan.get("meta", {}).get("source")
            report["stages"]["director"]["model_used"] = plan.get("meta", {}).get("model_used")
            report["stages"]["director"]["shot_count"] = len(plan.get("shots", []))
    except Exception:
        _write_report(report)
        return report

    # --- Stage 2: compliance（NGワード検査） ---
    try:
        with _timed_stage(report, "compliance"):
            ng_words = resolve_ng_words(cfg)
            check = compliance.check_plan(plan, ng_words=ng_words)
            (run_dir / "compliance_report.json").write_text(
                json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["stages"]["compliance"]["violations"] = check["violations"]
            report["stages"]["compliance"]["warnings"] = check["warnings"]
            if not check["ok"]:
                raise StageError(
                    "コンプライアンス違反のためrunを停止しました: {}".format(check["violations"])
                )
    except Exception:
        _write_report(report)
        return report

    shots = plan.get("shots", [])
    total_shot_duration = sum(s["duration_sec"] for s in shots)

    # --- Stage 3: 各ショットのビジュアル生成 + 正規化 ---
    clip_paths = []
    try:
        with _timed_stage(report, "visual"):
            backend = get_backend(backend_name, cfg)
            clips_dir = run_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            per_shot_meta = []
            for shot in shots:
                raw_path = clips_dir / "{}.raw.mp4".format(shot["id"])
                norm_path = clips_dir / "{}.mp4".format(shot["id"])
                meta = backend.generate(shot, str(raw_path))
                cmd = render.build_normalize_clip_cmd(
                    cfg["ffmpeg_bin"], str(raw_path), str(norm_path), duration_sec=shot["duration_sec"]
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise StageError(
                        "ショット{}の正規化に失敗: {}".format(shot["id"], res["stderr"][-500:])
                    )
                clip_paths.append(str(norm_path))
                per_shot_meta.append(meta)
            report["stages"]["visual"]["backend"] = backend.name
            report["stages"]["visual"]["clip_count"] = len(clip_paths)
    except Exception:
        _write_report(report)
        return report

    # --- Stage 4: concat ---
    concat_video_path = run_dir / "concat.mp4"
    try:
        with _timed_stage(report, "concat"):
            list_path = run_dir / "list.txt"
            list_path.write_text(render.build_concat_list_content(clip_paths), encoding="utf-8")
            cmd = render.build_concat_cmd(cfg["ffmpeg_bin"], str(list_path), str(concat_video_path))
            res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
            if res["returncode"] != 0:
                raise StageError("concatに失敗: {}".format(res["stderr"][-500:]))
    except Exception:
        _write_report(report)
        return report

    # --- Stage 5: TTS（ナレーション音声） ---
    narration_wav_path = run_dir / "narration.wav"
    try:
        with _timed_stage(report, "tts"):
            tts_backend = tts_mod.get_tts_backend(voice=cfg.get("voice", "Kyoko"))
            tts_meta = tts_backend.synthesize(plan.get("narration_script", ""), str(narration_wav_path), cfg)
            report["stages"]["tts"]["backend"] = tts_meta.get("backend")
            report["stages"]["tts"]["duration_sec"] = tts_meta.get("duration_sec")
            report["stages"]["tts"]["is_silent"] = tts_meta.get("is_silent")
    except Exception:
        _write_report(report)
        return report

    # --- Stage 6: 字幕(ASS)生成 ---
    ass_path = run_dir / "subtitles.ass"
    try:
        with _timed_stage(report, "subtitles"):
            hook_shot_id = shots[0]["id"] if shots else None
            telop_pieces = subtitles.build_telop_pieces_from_shots(shots, hook_shot_id=hook_shot_id)
            ass_text = subtitles.generate_ass(telop_pieces)
            ass_path.write_text(ass_text, encoding="utf-8")
            report["stages"]["subtitles"]["piece_count"] = len(telop_pieces)
    except Exception:
        _write_report(report)
        return report

    # --- Stage 7: 最終レンダリング（字幕焼込＋BGMダッキング＋loudnorm 2パス） ---
    output_path = None
    try:
        with _timed_stage(report, "render"):
            bgm_path = _resolve_bgm(plan.get("bgm_mood"))
            report["stages"]["render"]["bgm_mood"] = plan.get("bgm_mood")
            report["stages"]["render"]["bgm_path"] = bgm_path

            out_duration = total_shot_duration
            pass1_path = run_dir / "_loudnorm_pass1.mp4"
            cmd1 = render.build_final_cmd(
                cfg["ffmpeg_bin"], str(concat_video_path), str(narration_wav_path), str(pass1_path),
                str(ass_path), str(_FONTS_DIR), bgm_path=bgm_path, out_duration=out_duration,
                loudnorm_measured=None,
            )
            res1 = render.run_ffmpeg(cmd1, timeout_sec=_FFMPEG_TIMEOUT_SEC)
            measured = _parse_loudnorm_json(res1["stderr"])
            try:
                if pass1_path.exists():
                    pass1_path.unlink()
            except Exception:
                pass

            output_path = project_root() / "output" / "{}.mp4".format(run_id)
            (project_root() / "output").mkdir(parents=True, exist_ok=True)
            if res1["returncode"] != 0 or measured is None:
                cmd2 = render.build_final_cmd(
                    cfg["ffmpeg_bin"], str(concat_video_path), str(narration_wav_path), str(output_path),
                    str(ass_path), str(_FONTS_DIR), bgm_path=bgm_path, out_duration=out_duration,
                    loudnorm_measured=None,
                )
            else:
                cmd2 = render.build_final_cmd(
                    cfg["ffmpeg_bin"], str(concat_video_path), str(narration_wav_path), str(output_path),
                    str(ass_path), str(_FONTS_DIR), bgm_path=bgm_path, out_duration=out_duration,
                    loudnorm_measured=measured,
                )
            res2 = render.run_ffmpeg(cmd2, timeout_sec=_FFMPEG_TIMEOUT_SEC)
            if res2["returncode"] != 0:
                raise StageError("最終レンダリングに失敗: {}".format(res2["stderr"][-800:]))
            report["stages"]["render"]["loudnorm_measured"] = measured
            report["output_path"] = str(output_path)
    except Exception:
        _write_report(report)
        return report

    # --- Stage 8: QA ---
    try:
        with _timed_stage(report, "qa"):
            qa_report = qa_check.run_qa(
                cfg["ffmpeg_bin"], cfg["ffprobe_bin"], str(output_path), target_duration_sec=out_duration
            )
            report["qa"] = qa_report
            report["stages"]["qa"]["overall_ok"] = qa_report["overall_ok"]
            # QA不合格ならこのstage自体をokにしない（=report["ok"]もFalseになりexit code非0になる）。
            report["stages"]["qa"]["ok"] = qa_report["overall_ok"]
    except Exception:
        _write_report(report)
        return report

    report["ok"] = all(v.get("ok", False) for v in report["stages"].values())
    _write_report(report)
    return report


def _write_report(report):
    out_dir = project_root() / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="higgsfield-auto-reel: テーマ→9:16完成リール全自動パイプライン")
    parser.add_argument("--theme", required=True, help="動画のテーマ（例: 'AIで副業を始める最初の一歩'）")
    parser.add_argument("--duration", type=float, default=None, help="目標尺(秒)。未指定はconfig.jsonのtarget_duration_sec")
    parser.add_argument("--backend", default=None, choices=["mock", "higgsfield", "cloudapi"], help="ビジュアル生成バックエンド")
    parser.add_argument("--aspect", default="9:16", choices=["9:16"], help="現状9:16のみ対応")
    parser.add_argument("--no-llm", action="store_true", help="claude CLIを使わず決定論的テンプレートで企画生成する")
    args = parser.parse_args(argv)

    cfg = load_config()
    target_duration_sec = args.duration if args.duration is not None else cfg.get("target_duration_sec", 30)
    backend_name = args.backend or cfg.get("backend", "mock")

    report = run_pipeline(args.theme, target_duration_sec, backend_name, args.no_llm, cfg)

    print("[run] run_id={}".format(report.get("run_id")))
    for name, info in report["stages"].items():
        status = "OK" if info.get("ok") else "FAIL"
        print("  - {}: {} ({}s){}".format(name, status, info.get("elapsed_sec"), "" if info.get("ok") else " error=" + str(info.get("error"))))
    if report.get("output_path"):
        print("[run] output: {}".format(report["output_path"]))
    if report.get("qa"):
        print("[run] qa overall_ok={}".format(report["qa"]["overall_ok"]))
    print("[run] report: {}".format(project_root() / "output" / "report.json"))

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
