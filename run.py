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
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from pipeline.config import load_config, project_root
from pipeline import director
from pipeline import compliance
from pipeline import edit_profile
from pipeline import reference_v2
from pipeline import subtitles
from pipeline import render
from pipeline import tts as tts_mod
from pipeline import voice_catalog
from pipeline.visual import get_backend
from qa import qa_check

_ASSETS_DIR = project_root() / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"
_BGM_MANIFEST = _ASSETS_DIR / "bgm" / "manifest.json"
_FFMPEG_TIMEOUT_SEC = 1800

_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

# テロップアニメーション解決のため参照する編集プロファイル名（studio/server/jobs.py と共通）。
_TELOP_PROFILE_NAME = "ttp_reference"


def _resolve_animation_enabled():
    """編集プロファイルの telop.animation=="none" を尊重してテロップのアニメ有無を返す。

    プロファイル読み込み失敗時は True（従来のアニメ有りへフォールバック）。
    """
    try:
        from premiere.profile import load_profile as _load_full_profile
        _profile = _load_full_profile(_TELOP_PROFILE_NAME) or {}
        if ((_profile.get("telop") or {}).get("animation")) == "none":
            return False
    except Exception:
        pass
    return True


def _scale_shot_for_display(shot, display_duration_sec):
    """BUG-55: shot の duration_sec を display_duration_sec に付け替えつつ、
    caption_in_offset_sec / caption_out_offset_sec を同じ比で線形スケールする(shot 表示尺の
    境界内に caption が収まるようにする)。原尺=0/None のときはスケール比=1で通す。
    """
    result = dict(shot)
    result["duration_sec"] = display_duration_sec
    original = shot.get("duration_sec")
    if original and original > 0 and abs(display_duration_sec - original) > 1e-6:
        scale = display_duration_sec / original
        for key in ("caption_in_offset_sec", "caption_out_offset_sec"):
            v = shot.get(key)
            if v is None:
                continue
            result[key] = float(v) * scale
    return result


def _build_display_scaled_plan(plan, shot_display_durations):
    """BUG-55: plan.shots と plan.sfx_plan の shot 内 offset を表示尺スケールで補正した plan コピーを返す。
    元 plan は変更しない(compute_edit_enhancement_kwargs / sfx_planner 側で使う一時的コピー)。
    """
    if not isinstance(plan, dict):
        return plan
    scaled = dict(plan)
    shots = plan.get("shots") or []
    id_to_scale = {}
    scaled_shots = []
    for s in shots:
        sid = s.get("id")
        new_dur = shot_display_durations.get(sid, s.get("duration_sec"))
        original = s.get("duration_sec") or 0.0
        try:
            new_dur_f = float(new_dur)
        except (TypeError, ValueError):
            new_dur_f = original
        scale = (new_dur_f / original) if original and original > 0 else 1.0
        id_to_scale[sid] = scale
        scaled_shots.append(_scale_shot_for_display(s, new_dur_f))
    scaled["shots"] = scaled_shots
    if plan.get("sfx_plan") is not None:
        scaled_sfx = []
        for ev in plan.get("sfx_plan") or []:
            if not isinstance(ev, dict):
                continue
            anc = ev.get("t_anchor") or {}
            sid = anc.get("shot_id")
            scale = id_to_scale.get(sid, 1.0)
            new_anc = dict(anc)
            if anc.get("offset_sec") is not None:
                try:
                    new_anc["offset_sec"] = float(anc.get("offset_sec", 0.0)) * scale
                except (TypeError, ValueError):
                    pass
            new_ev = dict(ev, t_anchor=new_anc)
            scaled_sfx.append(new_ev)
        scaled["sfx_plan"] = scaled_sfx
    return scaled


def _slugify(theme, max_len=24):
    s = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー]+", "-", theme or "reel").strip("-")
    return (s[:max_len] or "reel")


def _new_run_dir(theme):
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + "-" + _slugify(theme)
    run_dir = project_root() / "work" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def _resolve_bgm(mood, seed=None, project_id=None, target_bpm=None):
    """ムードから BGM の絶対パスを返す（pipeline.bgm_library.pick_bgm 経由）。

    後方互換: 従来と同様の str(path) or None を返す。
    seed / project_id で決定論選曲＋直近使用2曲回避（"BGMが毎回同じ" 対策）。
    target_bpm(P0-3): 参考動画の実測 bpm。指定すると mood 適合曲の中から ±15BPM の
    曲を最優先で選ぶ（参考のテンポに準拠）。
    """
    if not mood or mood == "none":
        return None
    try:
        from pipeline import bgm_library
    except Exception:
        bgm_library = None
    if bgm_library is None:
        return None
    entry = bgm_library.pick_bgm(mood, seed=(seed if seed is not None else project_id),
                                 record_project_id=project_id, target_bpm=target_bpm)
    if not entry:
        return None
    fname = entry.get("file", "")
    if not fname:
        return None
    p = _ASSETS_DIR / "bgm" / fname
    return str(p) if p.exists() else None


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


def _load_fish_audio_key_from_secrets():
    """~/.claude/secrets/fish_audio_key があれば FISH_AUDIO_API_KEY 環境変数へ載せる。

    studio/start_server.sh と同じ方式で CLI 起動時にもキーを読み込む（キー値のログ出力は禁止・
    存在有無だけ True/False で返す）。既に環境変数が設定されていれば上書きしない。
    """
    if os.environ.get("FISH_AUDIO_API_KEY"):
        return True
    key_file = Path.home() / ".claude" / "secrets" / "fish_audio_key"
    try:
        if key_file.is_file():
            content = key_file.read_text(encoding="utf-8").strip()
            if content:
                os.environ["FISH_AUDIO_API_KEY"] = content
                return True
    except Exception:
        pass
    return False


def _make_local_file_fetcher(local_path):
    """--reference-file 指定時に reference_v2.default_fetch_video を差し替えるフェッチャを返す。

    URL の代わりにローカル mp4 を採用し、同梱 ffmpeg で音声を分離する。tmp_dir を作って
    その中に video.mp4 / audio.m4a を配置し、analyze_reference_v2 の想定と揃える
    （tmp_dir は解析後に _cleanup() で削除される）。
    """
    import shutil as _shutil
    import tempfile as _tempfile

    local_abs = Path(local_path).expanduser().resolve()

    def _fetch(url_ignored, cfg=None):
        cfg = cfg or {}
        if not local_abs.is_file():
            raise RuntimeError("--reference-file が存在しません: {}".format(local_abs))
        ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        ffprobe_bin = cfg.get("ffprobe_bin") or str(project_root() / "bin" / "ffprobe")
        tmp_dir = _tempfile.mkdtemp(prefix=reference_v2._TMP_DIR_CLEANUP_PREFIX_V2)
        try:
            video_path = str(Path(tmp_dir) / "video.mp4")
            _shutil.copyfile(str(local_abs), video_path)
            audio_path = str(Path(tmp_dir) / "audio.m4a")
            try:
                reference_v2._extract_audio(ffmpeg_bin, video_path, audio_path, copy=True)
            except Exception:
                reference_v2._extract_audio(ffmpeg_bin, video_path, audio_path, copy=False)
            from pipeline import reference as ref_v1
            duration = ref_v1._probe_duration(ffprobe_bin, video_path) or 0.0
            return {
                "video_path": video_path,
                "audio_path": audio_path,
                "duration_sec": duration,
                "tmp_dir": tmp_dir,
            }
        except Exception:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    return _fetch


class TTPReferenceMissingCLIError(SystemExit):
    """CLI 起動時に --reference-url / --reference-file が無く LLM 経路が呼ばれたことを示す（exit=2）。"""


def _emit_reference_required_error(theme):
    msg = (
        "参考動画URLが指定されていません。TTP v2 移行後、LLM 経路の企画生成には参考動画が必須です。\n\n"
        "使い方の例:\n"
        "  python run.py --theme \"{theme}\" --duration 30 --backend mock \\\n"
        "    --reference-url \"https://www.tiktok.com/@example/video/123\"\n\n"
        "  （ネットワーク不能な環境ではローカルの参考動画を渡せます）\n"
        "  python run.py --theme \"{theme}\" --duration 30 --backend mock \\\n"
        "    --reference-file /path/to/reference.mp4\n\n"
        "  （テスト・スモーク用の決定論的テンプレ生成なら）\n"
        "  python run.py --theme \"{theme}\" --duration 30 --backend mock --no-llm"
    ).format(theme=theme)
    print("[run] error: " + msg, file=sys.stderr)


def resolve_ng_words(cfg):
    """config の brand_rules.ng_words を「デフォルトNGワードへの追加分」として解決する。

    config未設定・ng_words未設定・ng_words=[] のいずれでも、compliance.DEFAULT_NG_WORDS
    （景表法NG表現・競合名義等）は常に有効にする。config側の値は追加分としてマージする。
    """
    config_ng_words = (cfg.get("brand_rules") or {}).get("ng_words") or []
    ng_words = list(compliance.get_effective_ng_words())
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


def _emit_premiere_package_from_cli(run_dir, plan, shot_display_durations, edit_sfx, bgm_curve,
                                       clip_paths, bgm_path, narration_wav_path, run_id):
    """CLI 経路 (--premiere-export) 用の Premiere パッケージ書き出し。

    Studio の projects/ 経路を使わず、run_dir 直下に premiere_package/ を作って
    reel.xml / timeline.json / captions.srt を書き出す。V1 の clip_path は run_dir/clips/
    配下の正規化済み mp4 を絶対パスで参照する（Premiere取り込み時に再リンク不要）。

    ここでは build_package() は使わず、export_xmeml.build_xmeml() を直接叩く
    （Studio project システムに依存させないため）。
    """
    from premiere import export_xmeml as _xe
    from premiere import srt as _srt
    from premiere import profile as _pmod

    pkg_dir = Path(run_dir) / "premiere_package"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Studio 形式の shots を作る（enabled=True・order は plan.shots 順・clip_path は絶対パス）。
    enabled_shots = []
    for i, s in enumerate(plan.get("shots") or []):
        clip_abs = None
        if i < len(clip_paths):
            clip_abs = str(Path(clip_paths[i]).resolve())
        dur = float(shot_display_durations.get(s["id"], s.get("duration_sec", 0.0)) or 0.0)
        enabled_shots.append({
            "id": s["id"], "order": i, "enabled": True,
            "caption": s.get("caption") or s.get("caption_jp") or "",
            "caption_in_offset_sec": s.get("caption_in_offset_sec"),
            "caption_out_offset_sec": s.get("caption_out_offset_sec"),
            "clip_path": clip_abs or "",
            "source_duration": dur,
            "trim": {"start": 0.0, "end": dur},
        })
    studio_plan = {
        "shots": enabled_shots,
        "hook_end_shot_id": plan.get("hook_end_shot_id"),
        "cta_start_shot_id": plan.get("cta_start_shot_id"),
        "bgm": {"file": bgm_path, "gain_db": None} if bgm_path else None,
        "sfx": [],
    }
    durations = [float(shot_display_durations.get(s["id"], s.get("duration_sec", 0.0)) or 0.0)
                 for s in plan.get("shots") or []]

    try:
        prof = _pmod.load_profile(name="ttp_reference")
    except Exception:
        prof = {"version": 1, "structure": {}, "telop": {}, "emphasis": {"red_circle": False},
                "audio": {"sfx": True, "bgm_gain_db": -14}}

    xmeml_result = _xe.build_xmeml(
        studio_plan, str(Path(run_dir).parent), profile=prof,
        narration_path=str(narration_wav_path), bgm_path=bgm_path,
        shot_display_durations=durations,
        sfx_events=edit_sfx, bgm_curve=bgm_curve,
        emit_captions=True, return_timeline=True,
    )
    (pkg_dir / "reel.xml").write_text(xmeml_result["xmeml"], encoding="utf-8")
    (pkg_dir / "timeline.json").write_text(
        json.dumps(xmeml_result.get("timeline") or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (pkg_dir / "captions.srt").write_text(_srt.build_srt(studio_plan, fps=30), encoding="utf-8")
    # 手順の1行README（Studio 経路と同じ格納名でユーザが同じ操作を思い出せるように）
    (pkg_dir / "README_import.md").write_text(
        "# Premiereでの読み込み方\n\n"
        "1. Premiere Pro で「ファイル > 読み込み」→ `reel.xml` を選択\n"
        "2. プロジェクトパネルに現れた `reel_sequence` をダブルクリックで開く\n"
        "3. `captions.srt` をタイムラインへドラッグ（テキスト直挿し版がV2に既に入っています）\n"
        "\n※ 同期意図の機械可読サマリは `timeline.json` を参照。\n",
        encoding="utf-8",
    )
    return {"package_dir": str(pkg_dir), "warnings": xmeml_result.get("warnings") or []}


def run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default",
                  reference_url=None, reference_file=None, premiere_export=False,
                  match_reference_duration=False, plan_tier=None):
    from pipeline import plan_tier as _plan_tier_mod
    _tier = _plan_tier_mod.infer_tier(plan_tier, backend_name)
    report = {
        "theme": theme,
        "target_duration_sec": target_duration_sec,
        "backend": backend_name,
        # プラン（コース）と費用見積・実消費。mock（むりょうコース）は外部有料APIを呼ばないため
        # 実消費 0 コインが確定している事実を記録する（見積精度改善・完了報告の材料）。
        "plan_tier": _tier,
        "billing": {
            "plan_tier": _tier,
            "coins_estimated": _plan_tier_mod.estimate_coins(cfg, _tier, duration_sec=target_duration_sec)["coins"],
            "coins_actual": 0 if backend_name == "mock" else None,
        },
        "no_llm": no_llm,
        "stages": {},
        "output_path": None,
        "qa": None,
        "ok": False,
    }

    run_id, run_dir = _new_run_dir(theme)
    report["run_id"] = run_id
    report["run_dir"] = str(run_dir)

    # --- Stage 0: 参考動画解析（reference_spec v2 の取得。director 直前で行う） ---
    # LLM 経路(no_llm=False)では reference_url または reference_file が必須。無ければ
    # director.run_director が TTPReferenceRequiredError を送出するので、その前段でも
    # 分かりやすいエラーを出せるよう先にチェックする（実際の送出は director 側で行う）。
    reference_spec = None
    if reference_url or reference_file:
        try:
            with _timed_stage(report, "reference"):
                if reference_file:
                    fetch_video = _make_local_file_fetcher(reference_file)
                    ref_input_url = "file://{}".format(Path(reference_file).expanduser().resolve())
                else:
                    fetch_video = None
                    ref_input_url = reference_url
                ref_result = reference_v2.analyze_reference_v2(
                    ref_input_url, cfg=cfg, fetch_video=fetch_video,
                )
                if not ref_result.get("ok"):
                    raise StageError(
                        "参考動画の解析に失敗しました: {}".format(ref_result.get("error") or "unknown")
                    )
                reference_spec = ref_result.get("spec")
                # spec を run_dir へ保存（下流のデバッグ・再現性用）
                try:
                    (run_dir / "reference_spec.json").write_text(
                        json.dumps(reference_spec, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
                report["stages"]["reference"]["source"] = ref_result.get("source")
                report["stages"]["reference"]["cached"] = bool(ref_result.get("cached"))
                report["stages"]["reference"]["cuts_count"] = len(
                    (reference_spec or {}).get("cuts") or []
                )
                report["stages"]["reference"]["telops_count"] = len(
                    (reference_spec or {}).get("telops") or []
                )
                report["stages"]["reference"]["sfx_count"] = len(
                    (reference_spec or {}).get("sfx_events") or []
                )
                report["stages"]["reference"]["warnings"] = list(ref_result.get("warnings") or [])
                report["stages"]["reference"]["duration_sec"] = (
                    reference_spec or {}
                ).get("duration_sec")
                # F11: --match-reference-duration が指定されていれば target を参考尺に強制一致。
                # config.target_duration_match_reference (config.local からの上書き) でも同じ扱い。
                match_flag = bool(match_reference_duration) or bool((cfg or {}).get("target_duration_match_reference"))
                if match_flag:
                    spec_duration = (reference_spec or {}).get("duration_sec")
                    if isinstance(spec_duration, (int, float)) and spec_duration > 0:
                        target_duration_sec = float(spec_duration)
                        report["target_duration_sec"] = target_duration_sec
                        report["stages"]["reference"]["match_reference_duration"] = True
        except Exception:
            _write_report(report)
            return report

    # edit enhancement層（カット点SE/パンチイン/BGM音量カーブ/フックのインパクト）。
    # プロファイル読み込み自体が失敗しても従来レンダを継続する（edit_prof=Noneなら以降すべて無効化）。
    # 編集レシピ選択の入力: run_id は director 実行時にはまだ確定していないため、
    # レシピの反映は director 完了後（plan.bgm_mood が判明した後）に再ロードする。
    try:
        edit_prof = edit_profile.load_edit_profile(cfg)
    except Exception:
        edit_prof = None

    # --- Stage 1: director（企画生成） ---
    plan = None
    try:
        with _timed_stage(report, "director"):
            plan = director.run_director(
                theme, cfg, target_duration_sec=target_duration_sec, no_llm=no_llm, quality=quality, style=style,
                reference=reference_spec,
                # R2b申し送り対応: 3段直列(supreme_plus)の途中失敗からリカバリできるよう
                # 中間 plan を run_dir に随時保存する。既存プロファイル(supreme/single)にも影響なし。
                checkpoint_dir=run_dir,
                # R4: backend を渡してクレジット意識分割を Higgsfield 時のみに限定する。
                backend=backend_name,
            )
            (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            # TTPS モードなら台本レポート(Markdown)も同時保存する。
            # 生成は純関数（LLM 呼び出しなし）。抽象化ラダー等の LLM 節はスケルトンで空欄。
            try:
                if compliance.is_ttps_mode(cfg, plan=plan):
                    report_md = director.build_ttps_script_report(
                        plan, reference_spec=reference_spec, cfg=cfg,
                    )
                    (run_dir / "ttps_script_report.md").write_text(report_md, encoding="utf-8")
                    report["stages"]["director"]["ttps_script_report"] = str(run_dir / "ttps_script_report.md")
            except Exception as _e:  # レポート失敗は本流を止めない
                report["stages"]["director"]["ttps_report_error"] = str(_e)[:300]
            report["stages"]["director"]["source"] = plan.get("meta", {}).get("source")
            report["stages"]["director"]["model_used"] = plan.get("meta", {}).get("model_used")
            report["stages"]["director"]["quality"] = plan.get("meta", {}).get("quality")
            report["stages"]["director"]["style"] = plan.get("meta", {}).get("style", style)
            report["stages"]["director"]["shot_count"] = len(plan.get("shots", []))
    except Exception:
        _write_report(report)
        return report

    # director 完了後、plan.bgm_mood と run_id を渡して編集レシピを解決し直す。
    # プロジェクトごとに編集の性格を切り替えるための決定論選択。ロードに失敗しても
    # 従来レンダを継続する（後方互換）。
    try:
        edit_prof = edit_profile.load_edit_profile(
            cfg, project_seed=run_id, bgm_mood=plan.get("bgm_mood"),
        )
    except Exception:
        pass  # 既存の edit_prof のまま
    if isinstance(edit_prof, dict) and edit_prof.get("edit_recipe"):
        report["edit_recipe"] = edit_prof["edit_recipe"]

    # --- Stage 2: compliance（NGワード検査） ---
    try:
        with _timed_stage(report, "compliance"):
            ng_words = resolve_ng_words(cfg)
            ng_patterns = None
            # TTPS モードでは薬機法 / 景表法の追加 NG語と正規表現も足して検査する。
            if compliance.is_ttps_mode(cfg, plan=plan):
                ng_words = compliance.build_ttps_ng_words(base_ng_words=ng_words)
                ng_patterns = compliance.build_ttps_ng_patterns()
                report["stages"]["compliance"]["ttps_mode"] = True
            check = compliance.check_plan(plan, ng_words=ng_words, ng_patterns=ng_patterns)
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

    # --- Stage 3: 各ショットのビジュアル生成（raw取得のみ） ---
    # 正規化（trim/pad）はStage5（TTS）で音声主導タイミング同期モードの適用有無・各ショットの
    # 表示尺が確定してから行う（Stage4に統合。従来は生成直後に正規化していたが、同期モードでは
    # 断片TTSの実測尺が確定するまで各ショットの最終的な表示尺が決まらないため）。
    raw_clip_paths = {}
    try:
        with _timed_stage(report, "visual"):
            backend = get_backend(backend_name, cfg)
            clips_dir = run_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            per_shot_meta = []
            for shot in shots:
                raw_path = clips_dir / "{}.raw.mp4".format(shot["id"])
                meta = backend.generate(shot, str(raw_path))
                raw_clip_paths[shot["id"]] = str(raw_path)
                per_shot_meta.append(meta)
            report["stages"]["visual"]["backend"] = backend.name
            report["stages"]["visual"]["clip_count"] = len(raw_clip_paths)
            # 統合配線: persona_anchor（人物 shot の identity 統一）の発火分布を記録する。
            # has_person→reference_visual.has_person→backend が読む配線が実際に効いたかの証跡。
            _pa_counts = {}
            for _m in per_shot_meta:
                _pa = (_m or {}).get("persona_anchor")
                if _pa:
                    _pa_counts[_pa] = _pa_counts.get(_pa, 0) + 1
            if _pa_counts:
                report["stages"]["visual"]["persona_anchor"] = _pa_counts

            # ②検知（読み取りのみ・best-effort）: 生成した raw クリップ（テロップ焼き込み前）を
            # vision で検査し、映像内文字/文字化けの疑いを text_artifacts として report に記録する。
            # 失敗しても visual ステージは成功扱いのまま続行する（作り直しはユーザー判断）。
            # 対象は実映像を生成する higgsfield backend のみ（mock 等はAIが偽文字を描かないため
            # 検査不要。無駄な vision 呼び出しも避ける）。
            if getattr(backend, "name", "") == "higgsfield":
              try:
                from qa.clip_text_check import run_clip_text_check
                _tc_clips = [
                    {
                        "shot_id": s["id"],
                        "clip_path": raw_clip_paths.get(s["id"]),
                        "duration_sec": s.get("duration_sec"),
                    }
                    for s in shots
                    if raw_clip_paths.get(s["id"])
                ]
                _tc = run_clip_text_check(_tc_clips, ffmpeg_bin=str(cfg.get("ffmpeg_bin")), cfg=cfg)
                report["stages"]["visual"]["text_artifacts"] = _tc.get("text_artifacts", [])
                report["stages"]["visual"]["text_check"] = {
                    "ok": _tc.get("ok"),
                    "enabled": _tc.get("enabled"),
                    "checked_shot_ids": _tc.get("checked_shot_ids"),
                    "unchecked_shot_ids": _tc.get("unchecked_shot_ids"),
                    "calls_used": _tc.get("calls_used"),
                    "error": _tc.get("error"),
                }
              except Exception as _tc_exc:  # noqa: BLE001 — 検知失敗で本編を止めない
                report["stages"]["visual"]["text_check"] = {"ok": False, "error": str(_tc_exc)[:300]}
    except Exception:
        _write_report(report)
        return report

    # --- Stage 4: TTS（ナレーション音声。音声主導タイミング同期モードの判定を含む） ---
    # 同期モード: 全shotsにnarration_jpが非空で揃っている場合のみ試みる。
    # pipeline.tts.synthesize_segments()でショット単位に断片合成し、成功したら各ショットの
    # 表示尺 = render.compute_synced_shot_duration(断片実測尺) を採用する（テロップ/クリップ尺も
    # この値に統一するため、音声=テロップ=ショット境界が原理的に一致する）。
    # 断片TTSが1つでも失敗、またはnarration_jpが揃っていない場合は従来どおりの全文方式
    # （各ショットの表示尺はdirectorが立てたduration_secをそのまま使う）。
    narration_wav_path = run_dir / "narration.wav"
    tts_mode = "full"
    shot_display_durations = {}
    # 声TTP: 使う声を確定する。voice_mode="auto" なら参考の narrator_voice（話者推定）に
    # 最も近い声を現在の tier（free=say / paid=fish）から選ぶ。手動 key 指定ならその声。
    # narration_mode=absent（ナレ無し）のときは声は使われないが、記録のため解決だけしておく。
    _narrator_voice = (reference_spec or {}).get("narrator_voice") if isinstance(reference_spec, dict) else None
    voice_used = voice_catalog.resolve_voice(cfg, _narrator_voice)
    _tts_engine = voice_used.get("engine")
    _tts_evid = voice_used.get("engine_voice_id")
    _say_voice = _tts_evid if _tts_engine == "say" else cfg.get("voice", "Kyoko")
    _fish_ref = _tts_evid if _tts_engine == "fish" else None
    report["voice_used"] = {"key": voice_used.get("key"), "label": voice_used.get("label")}
    # P0-2: narration_mode="absent"（参考にナレーション無し）は TTS を丸ごとスキップし、
    # 尺ぴったりの無音トラックだけ用意する（BGM+テロップで成立・尺 drift 0）。
    narration_absent = (plan.get("narration_mode") == "absent") or (
        bool(shots)
        and all(not (isinstance(s.get("narration_jp"), str) and s.get("narration_jp").strip()) for s in shots)
        and not (isinstance(plan.get("narration_script"), str) and plan.get("narration_script").strip())
    )
    try:
        with _timed_stage(report, "tts"):
          if narration_absent:
            tts_mode = "none"
            for shot in shots:
                shot_display_durations[shot["id"]] = shot["duration_sec"]
            _absent_total = sum(shot["duration_sec"] for shot in shots) if shots else 0.0
            tts_meta = tts_mod.synthesize_silent_track(
                str(narration_wav_path), _absent_total, cfg
            )
            report["stages"]["tts"]["backend"] = tts_meta.get("backend")
            report["stages"]["tts"]["duration_sec"] = tts_meta.get("duration_sec")
            report["stages"]["tts"]["is_silent"] = True
            report["stages"]["tts"]["mode"] = "none"
            report["stages"]["tts"]["narration_mode"] = "absent"
          else:
            sync_eligible = bool(shots) and all(
                isinstance(s.get("narration_jp"), str) and s.get("narration_jp").strip() for s in shots
            )
            sync_result = None
            if sync_eligible:
                seg_dir = run_dir / "narration_segments"
                sync_result = tts_mod.synthesize_segments(
                    [s["narration_jp"].strip() for s in shots], str(seg_dir), cfg,
                    voice=_say_voice, engine=_tts_engine, fish_reference_id=_fish_ref,
                )

            if sync_result and sync_result.get("ok"):
                tts_mode = "segment"
                # F13: TTS 尺整合ガード（共通ヘルパ）。断片実尺が plan 尺を超えたら atempo(<=1.15)で
                # 圧縮吸収する。目標尺は plan - SYNC_GAP_SEC（gap 込みの表示尺で plan と一致させる）。
                adjusted_segments, tempo_adjustments = render.enforce_tempo_guard_on_segments(
                    sync_result["segments"],
                    plan_durations=[float(s.get("duration_sec") or 0.0) for s in shots],
                    ffmpeg_bin=cfg["ffmpeg_bin"],
                    ffprobe_bin=cfg.get("ffprobe_bin") or None,
                    shot_ids=[s["id"] for s in shots],
                )
                cursor = 0.0
                segment_specs = []
                for shot, seg in zip(shots, adjusted_segments):
                    seg_dur = float(seg["duration_sec"])
                    seg_path = seg["path"]
                    display = render.compute_synced_shot_duration(seg_dur)
                    shot_display_durations[shot["id"]] = display
                    segment_specs.append({"path": seg_path, "start_sec": cursor})
                    cursor += display
                cmd = render.build_narration_segments_concat_cmd(
                    cfg["ffmpeg_bin"], segment_specs, cursor, str(narration_wav_path)
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise StageError("ナレーション断片の合成に失敗: {}".format(res["stderr"][-500:]))
                tts_meta = {
                    "backend": sync_result.get("backend"), "duration_sec": cursor, "is_silent": False,
                    "requested_backend": sync_result.get("backend"),
                    "fallback_reason": sync_result.get("fallback_reason"), "mode": "segment",
                }
            else:
                for shot in shots:
                    shot_display_durations[shot["id"]] = shot["duration_sec"]
                tts_backend = tts_mod.get_tts_backend(
                    voice=_say_voice, cfg=cfg, engine=_tts_engine, fish_reference_id=_fish_ref,
                )
                tts_meta = dict(tts_backend.synthesize(plan.get("narration_script", ""), str(narration_wav_path), cfg))
                tts_meta["mode"] = "full"

            report["stages"]["tts"]["backend"] = tts_meta.get("backend")
            report["stages"]["tts"]["duration_sec"] = tts_meta.get("duration_sec")
            report["stages"]["tts"]["is_silent"] = tts_meta.get("is_silent")
            report["stages"]["tts"]["requested_backend"] = tts_meta.get("requested_backend")
            report["stages"]["tts"]["fallback_reason"] = tts_meta.get("fallback_reason")
            report["stages"]["tts"]["mode"] = tts_meta.get("mode")
            # F13: TTS 尺整合ガードの適用履歴（segment モードのみ・非空のとき記録）
            if tts_mode == "segment":
                try:
                    if tempo_adjustments:
                        report["stages"]["tts"]["tempo_adjustments"] = tempo_adjustments
                        fully = sum(1 for a in tempo_adjustments if a.get("fully_absorbed"))
                        report["stages"]["tts"]["tempo_adjustments_summary"] = {
                            "count": len(tempo_adjustments),
                            "fully_absorbed": fully,
                            "residual_over_plan": len(tempo_adjustments) - fully,
                            "max_tempo": render.TEMPO_GUARD_MAX_TEMPO,
                        }
                except NameError:  # tempo_adjustments 未定義（安全ガード）
                    pass
    except Exception:
        _write_report(report)
        return report

    # --- Stage 5: 正規化 + concat（各ショットをshot_display_durationsの尺へ揃えて連結） ---
    clip_paths = []
    concat_video_path = run_dir / "concat.mp4"
    try:
        with _timed_stage(report, "concat"):
            for shot_index, shot in enumerate(shots):
                raw_path = raw_clip_paths[shot["id"]]
                norm_path = clips_dir / "{}.mp4".format(shot["id"])
                target_duration = shot_display_durations.get(shot["id"], shot["duration_sec"])
                duration_sec, pad_to_duration_sec = render.resolve_normalize_pad_args(
                    shot["duration_sec"], target_duration if tts_mode == "segment" else None
                )
                punch_in_filter = None
                if edit_prof is not None:
                    try:
                        punch_in_filter = render.resolve_punch_in_filter_for_shot(
                            shot_index, target_duration, edit_prof, backend_name
                        )
                    except Exception:
                        punch_in_filter = None
                cmd = render.build_normalize_clip_cmd(
                    cfg["ffmpeg_bin"], raw_path, str(norm_path), duration_sec=duration_sec,
                    pad_to_duration_sec=pad_to_duration_sec, punch_in_filter=punch_in_filter,
                )
                res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
                if res["returncode"] != 0:
                    raise StageError(
                        "ショット{}の正規化に失敗: {}".format(shot["id"], res["stderr"][-500:])
                    )
                clip_paths.append(str(norm_path))

            list_path = run_dir / "list.txt"
            list_path.write_text(render.build_concat_list_content(clip_paths), encoding="utf-8")
            cmd = render.build_concat_cmd(cfg["ffmpeg_bin"], str(list_path), str(concat_video_path))
            res = render.run_ffmpeg(cmd, timeout_sec=_FFMPEG_TIMEOUT_SEC)
            if res["returncode"] != 0:
                raise StageError("concatに失敗: {}".format(res["stderr"][-500:]))
    except Exception:
        _write_report(report)
        return report

    # --- Stage 6: 字幕(ASS)生成（表示尺=shot_display_durationsをショットのduration_secへ反映） ---
    ass_path = run_dir / "subtitles.ass"
    try:
        with _timed_stage(report, "subtitles"):
            hook_shot_id = shots[0]["id"] if shots else None
            # BUG-55: 表示尺(shot_display_durations)がplan の duration_sec と異なる場合、
            # caption_in_offset_sec / caption_out_offset_sec もその比で線形にスケールする。
            # スケールしないと caption 終了時刻が次shot に食い込み表示重なりが発生する。
            telop_shots = [
                _scale_shot_for_display(s, shot_display_durations.get(s["id"], s["duration_sec"])) for s in shots
            ]
            telop_pieces = subtitles.build_telop_pieces_from_shots(telop_shots, hook_shot_id=hook_shot_id)
            # TTPS モード: #PR テロップ全編表示 & 効果言及shotへの「※効果には個人差があります」自動注記。
            # 有効判定は cfg.ttps.enabled / plan.pr_disclosure / plan.meta.product のいずれか
            # （compliance.is_ttps_mode で集約）。無効時は追加pieceなし＝完全後方互換。
            ttps_enabled = compliance.is_ttps_mode(cfg, plan=plan)
            pr_text = plan.get("pr_disclosure")
            pr_cfg = ((cfg or {}).get("compliance") or {}).get("pr_disclosure") or {}
            if pr_text is None and ttps_enabled and pr_cfg.get("enabled", True):
                pr_text = pr_cfg.get("text") or compliance.PR_DISCLOSURE_DEFAULT
            if pr_text:
                total_dur = sum(
                    float(shot_display_durations.get(s["id"], s["duration_sec"]) or 0.0)
                    for s in shots
                )
                pr_piece = subtitles.build_pr_disclosure_piece(total_dur, pr_text=pr_text)
                if pr_piece:
                    telop_pieces.append(pr_piece)
                    report["stages"]["subtitles"]["pr_disclosure"] = pr_text
            # 効果言及shotへの注記（TTPSモード＋自動注記が config で ON のときのみ）。
            variance_cfg = ((cfg or {}).get("compliance") or {}).get("variance_note") or {}
            if ttps_enabled and variance_cfg.get("enabled", True):
                hit_ids = compliance.detect_effect_mention_shot_ids(plan)
                if hit_ids:
                    note_pieces = subtitles.build_variance_note_pieces_for_shots(
                        telop_shots, hit_ids, note_text=variance_cfg.get("text"),
                    )
                    telop_pieces.extend(note_pieces)
                    report["stages"]["subtitles"]["variance_note_shots"] = hit_ids
            # 動画ごとに決定論的にテロップスタイルを1つ選ぶ（run_idをseedにするので同じ動画は同じスタイル）。
            # CLI経路は現状 vertical_hook プリセットの指定手段がないため preset は None で
            # horizontal_pool(7スタイル)から乱択する。
            telop_style_name = subtitles.pick_telop_style_name(
                run_id, preset=None, record_project_id=run_id,
            )
            ass_text = subtitles.generate_ass(
                telop_pieces, animation_enabled=_resolve_animation_enabled(),
                telop_style=telop_style_name,
            )
            ass_path.write_text(ass_text, encoding="utf-8")
            report["stages"]["subtitles"]["piece_count"] = len(telop_pieces)
            report["stages"]["subtitles"]["telop_style"] = telop_style_name
    except Exception:
        _write_report(report)
        return report

    # --- Stage 7: 最終レンダリング（字幕焼込＋BGMダッキング＋loudnorm 2パス） ---
    output_path = None
    try:
        with _timed_stage(report, "render"):
            # P0-3: 参考動画の実測 bpm を BGM 選定へ渡す（±15BPM 近傍を優先）。
            _ref_bpm = None
            _ref_music = (reference_spec or {}).get("music") if isinstance(reference_spec, dict) else None
            if isinstance(_ref_music, dict):
                _bpm_v = _ref_music.get("bpm")
                if isinstance(_bpm_v, (int, float)) and _bpm_v > 0:
                    _ref_bpm = float(_bpm_v)
            bgm_path = _resolve_bgm(plan.get("bgm_mood"), project_id=run_id, target_bpm=_ref_bpm)
            report["stages"]["render"]["bgm_mood"] = plan.get("bgm_mood")
            report["stages"]["render"]["bgm_reference_bpm"] = _ref_bpm
            report["stages"]["render"]["bgm_path"] = bgm_path

            out_duration = sum(shot_display_durations.get(s["id"], s["duration_sec"]) for s in shots) if shots else total_shot_duration

            # 尺乖離の可視化: director企画時点のショット合計尺(total_shot_duration=plan上の
            # duration_sec合計)と、同期モード後の実出力尺(out_duration)の差分を記録する。
            # |drift|>3.0秒ならstageメッセージ/reportに警告文字列を残す。QAの合否判定式
            # （qa/qa_check.py）は変えない（誤検知でrunを止めるリスクを避けるため。音声の
            # 実尺に合わせて意図的に尺が動くのは同期モードの仕様）。
            # 独立レビュー指摘: 閾値判定は丸め後の値ではなく生の差分で行う（例: 3.004秒が
            # round()で3.0になり閾値超えを見逃すのを防ぐ）。round()は記録用の表示値にのみ適用する。
            _raw_drift = out_duration - total_shot_duration
            duration_drift_sec = round(_raw_drift, 2)
            report["stages"]["render"]["duration_drift_sec"] = duration_drift_sec
            if abs(_raw_drift) > 3.0:
                direction = "長く" if _raw_drift > 0 else "短く"
                # ここでの基準はCLI引数のtarget_duration_secそのものではなく、director企画時点の
                # ショット合計尺(total_shot_duration)。「目標尺」ではなく「企画時点の尺」と表記し、
                # 数字上の実際の比較対象を正確に示す（jobs.py側はproject.target_duration_secを
                # 基準にするため、そちらは「目標尺」表記のままで正しい）。
                report["stages"]["render"]["duration_drift_warning"] = (
                    "※企画時点の尺より{:.1f}秒{}なりました（音声に合わせたため）".format(abs(_raw_drift), direction)
                )

            # edit enhancement層: カットSE(sfx)/BGM音量カーブ/フックのインパクトを求める。
            # この層の計算で例外が起きても従来レンダ(edit無し)へフォールバックする。
            edit_profile_applied = False
            edit_sfx = []
            bgm_curve = None
            first_shot_impact_sec = None
            main_dip_events = None
            if edit_prof is not None:
                try:
                    durations = [shot_display_durations.get(s["id"], s["duration_sec"]) for s in shots]
                    # BUG-55: SFX の shot 内 offset も表示尺スケールで補正する(caption と同機構)。
                    # sfx_planner は plan.shots[i].caption_in_offset_sec / t_anchor.offset_sec を
                    # そのまま使うため、原尺スケールのままだと SE が shot 外に飛ぶ。
                    scaled_plan = _build_display_scaled_plan(plan, shot_display_durations)
                    # R2b F6: SE 音色マッチング用の永続キャッシュ（無ければ内部で生成する）。
                    # librosa/soundfile が未導入なら SfxTimbreCache は空返しになるので影響なし。
                    _timbre_cache = None
                    if reference_spec is not None:
                        try:
                            from pipeline.sfx_matcher import SfxTimbreCache as _StC
                            _timbre_cache = _StC(
                                cache_path=str(project_root() / "assets" / "sfx" / "manifest_mfcc.json")
                            )
                        except Exception:
                            _timbre_cache = None
                    enhancement = render.compute_edit_enhancement_kwargs(
                        durations, edit_prof, project_seed=run_id, plan=scaled_plan,
                        reference_spec=reference_spec, sfx_timbre_cache=_timbre_cache,
                    )
                    # timbre cache が新規計算した MFCC を JSON に永続化（次回以降 skip）
                    if _timbre_cache is not None:
                        try:
                            _timbre_cache.save()
                        except Exception:
                            pass
                    edit_sfx = enhancement["sfx_extra"]
                    bgm_curve = enhancement["bgm_curve"]
                    first_shot_impact_sec = enhancement["first_shot_impact_sec"]
                    main_dip_events = enhancement.get("main_dip_events")
                    edit_profile_applied = True
                except Exception:
                    edit_sfx = []
                    bgm_curve = None
                    first_shot_impact_sec = None
                    main_dip_events = None
                    edit_profile_applied = False
            report["stages"]["render"]["edit_profile_applied"] = edit_profile_applied

            pass1_path = run_dir / "_loudnorm_pass1.mp4"
            cmd1 = render.build_final_cmd(
                cfg["ffmpeg_bin"], str(concat_video_path), str(narration_wav_path), str(pass1_path),
                str(ass_path), str(_FONTS_DIR), bgm_path=bgm_path, out_duration=out_duration,
                loudnorm_measured=None, sfx=edit_sfx, bgm_curve=bgm_curve,
                first_shot_impact_sec=first_shot_impact_sec,
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
                    loudnorm_measured=None, sfx=edit_sfx, bgm_curve=bgm_curve,
                    first_shot_impact_sec=first_shot_impact_sec,
                    main_dip_events=main_dip_events,
                )
            else:
                cmd2 = render.build_final_cmd(
                    cfg["ffmpeg_bin"], str(concat_video_path), str(narration_wav_path), str(output_path),
                    str(ass_path), str(_FONTS_DIR), bgm_path=bgm_path, out_duration=out_duration,
                    loudnorm_measured=measured, sfx=edit_sfx, bgm_curve=bgm_curve,
                    first_shot_impact_sec=first_shot_impact_sec,
                    main_dip_events=main_dip_events,
                )
            res2 = render.run_ffmpeg(cmd2, timeout_sec=_FFMPEG_TIMEOUT_SEC)
            if res2["returncode"] != 0:
                raise StageError("最終レンダリングに失敗: {}".format(res2["stderr"][-800:]))
            report["stages"]["render"]["loudnorm_measured"] = measured
            report["output_path"] = str(output_path)
    except Exception:
        _write_report(report)
        return report

    # --- Stage 7.5: Premiere パッケージ書き出し（--premiere-export） ---
    if premiere_export:
        try:
            with _timed_stage(report, "premiere_export"):
                pkg = _emit_premiere_package_from_cli(
                    run_dir, plan, shot_display_durations, edit_sfx, bgm_curve,
                    clip_paths, bgm_path, narration_wav_path, run_id,
                )
                report["stages"]["premiere_export"]["package_dir"] = pkg["package_dir"]
                report["stages"]["premiere_export"]["warnings"] = pkg["warnings"]
        except Exception:
            # ここが失敗してもレンダ本体は既に成功しているため、report を書いて先へ進む
            pass

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

    # --- Stage 9: fidelity (F13: 参考動画TTPの再現度指標) ---
    # reference_spec があるときのみ実行。生成 plan と参考spec を突き合わせて 5指標を
    # 出し、output/<run_id>/fidelity.json に保存する（機械可読・以降の改善が
    # 定量的に測れる状態を作る）。
    if reference_spec is not None:
        try:
            with _timed_stage(report, "fidelity"):
                from qa.fidelity import compute_fidelity
                fid = compute_fidelity(reference_spec, plan)
                (run_dir / "fidelity.json").write_text(
                    json.dumps(fid, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                report["fidelity"] = fid["summary"]
                report["stages"]["fidelity"]["ok"] = True
        except Exception as exc:
            # fidelity は補助指標。失敗しても本体成功を潰さない。
            report["stages"].setdefault("fidelity", {})["ok"] = False
            report["stages"]["fidelity"]["error"] = str(exc)[:300]

    report["ok"] = all(v.get("ok", False) for v in report["stages"].values())
    _write_report(report)
    return report


def _write_report(report):
    out_dir = project_root() / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    # 既存互換: output/report.json は「最新1本」の意味で維持する。
    (out_dir / "report.json").write_text(payload, encoding="utf-8")
    # run_id ごとの永続保存: output/reports/<run_id>.json を追加で書き出す（複数本の並列/連続実行時、
    # report.json が最新1本で上書きされて過去 run のトレースを失う問題への対処）。
    run_id = report.get("run_id")
    if run_id:
        reports_dir = out_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "{}.json".format(run_id)).write_text(payload, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="higgsfield-auto-reel: テーマ→9:16完成リール全自動パイプライン")
    parser.add_argument("--theme", required=True, help="動画のテーマ（例: 'AIで副業を始める最初の一歩'）")
    parser.add_argument("--duration", type=float, default=None, help="目標尺(秒)。未指定はconfig.jsonのtarget_duration_sec")
    parser.add_argument("--backend", default=None, choices=["mock", "higgsfield", "cloudapi"], help="ビジュアル生成バックエンド（上級者向け。--plan 指定時は --plan が優先）")
    parser.add_argument(
        "--plan", default=None, choices=["free", "paid"],
        help="プラン（ワンセット）。free=むりょうコース（映像mock/声say/文字起こしスキップ・0円保証） / "
             "paid=本番コース（映像higgsfield/声Fish Audio/文字起こしFish Audio ASR）。"
             "指定時は --backend より優先し、TTS/ASR もまとめて切り替える",
    )
    parser.add_argument("--aspect", default="9:16", choices=["9:16"], help="現状9:16のみ対応")
    parser.add_argument("--no-llm", action="store_true", help="claude CLIを使わず決定論的テンプレートで企画生成する")
    parser.add_argument(
        "--quality", default=None, choices=["supreme", "single", "supreme_plus"],
        help="AIディレクターの生成品質。supreme=write+polish(既定) / single=一発出し / "
             "supreme_plus=write+polish+rewrite の3段(F12: 品質最優先。config.local/quality_max.json と併用)",
    )
    parser.add_argument(
        "--match-reference-duration", action="store_true",
        help="F11: target_duration_sec を参考動画の実尺(reference.duration_sec)に強制一致させる。"
             "指定時は --duration より優先。参考のリズム(高速カット/長回し)をそのまま保つのに使う",
    )
    parser.add_argument(
        "--style", default="default", choices=["default", "vertical_hook"],
        help="AIディレクターの企画スタイル。default=従来の企画・カット割り(既定) / "
             "vertical_hook=縦書きテロップ・高速カット向けのTTP構成",
    )
    parser.add_argument(
        "--reference-url", default=None,
        help="参考動画URL（TikTok等）。TTP v2 移行後、LLM経路の企画生成には必須（--no-llm 指定時は不要）",
    )
    parser.add_argument(
        "--reference-file", default=None,
        help="参考動画のローカルmp4パス。DL不能な環境で --reference-url の代わりに使う",
    )
    parser.add_argument(
        "--premiere-export", action="store_true",
        help="レンダ完了後、Premiere Pro読み込み用パッケージ(reel.xml/timeline.json/captions.srt)を "
             "output/<run_id>/premiere_package/ に書き出す（Phase B）",
    )
    args = parser.parse_args(argv)

    # Fish Audio API キー: ~/.claude/secrets/fish_audio_key があれば環境変数へ載せる
    # （キー値のログ出力は禁止・存在有無だけ内部で保持）。
    _load_fish_audio_key_from_secrets()

    cfg = load_config()
    target_duration_sec = args.duration if args.duration is not None else cfg.get("target_duration_sec", 30)

    # プラン（ワンセット）: --plan は --backend より優先し、映像backend・TTSエンジン・ASR有無を
    # まとめて切り替える（free=0円保証 / paid=有料一式）。両方指定で矛盾（例: --plan free --backend
    # higgsfield）していたら明示エラーで止める（暗黙にどちらかを勝たせると事故る）。
    from pipeline import plan_tier as plan_tier_mod
    plan_tier = plan_tier_mod.normalize_tier(args.plan)
    if plan_tier is not None:
        resolved_backend = plan_tier_mod.resolve_backend(plan_tier, None)
        if args.backend is not None and args.backend != resolved_backend:
            print(
                "エラー: --plan {} は映像バックエンド '{}' を使います（--backend {} と矛盾します）。"
                "どちらか一方だけを指定してください。".format(plan_tier, resolved_backend, args.backend)
            )
            return 2
        backend_name = resolved_backend
        cfg = plan_tier_mod.apply_tier_to_cfg(cfg, plan_tier)
    else:
        backend_name = args.backend or cfg.get("backend", "mock")
    quality = args.quality or cfg.get("director_quality", "supreme")

    # TTP v2 モード必須ガード: reference_url/file が無く --no-llm でもない場合は
    # 分かりやすい日本語エラー+使用例を表示して exit 2。
    if not args.no_llm and not args.reference_url and not args.reference_file:
        _emit_reference_required_error(args.theme)
        return 2

    try:
        report = run_pipeline(
            args.theme, target_duration_sec, backend_name, args.no_llm, cfg, quality=quality, style=args.style,
            reference_url=args.reference_url, reference_file=args.reference_file,
            premiere_export=args.premiere_export,
            match_reference_duration=args.match_reference_duration,
            plan_tier=plan_tier,
        )
    except director.TTPReferenceRequiredError:
        _emit_reference_required_error(args.theme)
        return 2

    print("[run] run_id={}".format(report.get("run_id")))
    for name, info in report["stages"].items():
        status = "OK" if info.get("ok") else "FAIL"
        print("  - {}: {} ({}s){}".format(name, status, info.get("elapsed_sec"), "" if info.get("ok") else " error=" + str(info.get("error"))))
    render_stage = report["stages"].get("render") or {}
    if render_stage.get("duration_drift_warning"):
        print("[run] warning: {}".format(render_stage["duration_drift_warning"]))
    if report.get("output_path"):
        print("[run] output: {}".format(report["output_path"]))
    if report.get("qa"):
        print("[run] qa overall_ok={}".format(report["qa"]["overall_ok"]))
    print("[run] report: {}".format(project_root() / "output" / "report.json"))

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
