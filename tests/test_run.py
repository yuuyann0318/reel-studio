# -*- coding: utf-8 -*-
"""run.py のstageロジック検証（BUG-3: QA失敗の伝播 / BUG-4: ng_wordsのデフォルト有効化）。"""
from pathlib import Path

import pytest

import run
from pipeline import compliance
from pipeline.config import load_config


# ---------------------------------------------------------------------------
# BUG-4: config.brand_rules.ng_words が未設定/空でもデフォルトNGワードは常に有効
# ---------------------------------------------------------------------------

def test_resolve_ng_words_uses_defaults_when_brand_rules_missing():
    ng_words = run.resolve_ng_words({})
    assert "絶対稼げる" in ng_words
    # DEFAULT_NG_WORDS(景表法系)が常に有効であることを担保
    assert set(compliance.DEFAULT_NG_WORDS).issubset(set(ng_words))


def test_resolve_ng_words_uses_defaults_when_ng_words_key_missing():
    ng_words = run.resolve_ng_words({"brand_rules": {}})
    assert "絶対稼げる" in ng_words


def test_resolve_ng_words_uses_defaults_when_ng_words_is_empty_list():
    ng_words = run.resolve_ng_words({"brand_rules": {"ng_words": []}})
    assert "絶対稼げる" in ng_words
    assert set(compliance.DEFAULT_NG_WORDS).issubset(set(ng_words))


def test_resolve_ng_words_merges_config_additions_with_defaults():
    ng_words = run.resolve_ng_words({"brand_rules": {"ng_words": ["カスタムNGワード"]}})
    assert "カスタムNGワード" in ng_words
    assert "絶対稼げる" in ng_words


def test_compliance_blocks_ng_theme_even_without_config_ng_words():
    """config側にng_wordsが無い状態でも、景表法NG表現「絶対稼げる」を含むplanが検査で弾かれる。"""
    ng_words = run.resolve_ng_words({"brand_rules": {}})
    plan = {"concept": "絶対稼げる副業術", "hook": "", "narration_script": "", "shots": []}
    check = compliance.check_plan(plan, ng_words=ng_words)
    assert check["ok"] is False
    assert any(v["word"] == "絶対稼げる" for v in check["violations"])


# ---------------------------------------------------------------------------
# BUG-3: QA不合格ならstage/reportのokがFalseになり、exit codeが非0になる
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_run_pipeline_qa_failure_makes_report_not_ok(monkeypatch):
    """qa_check.run_qaを差し替え、意図的にoverall_ok=Falseを返させて伝播を検証する
    (=尺を極端に外す等の実データ経路の代わりに、QA判定そのものを強制失敗させるテスト用手段)。"""
    cfg = load_config()

    def _fake_run_qa(ffmpeg_bin, ffprobe_bin, output_path, target_duration_sec=None):
        return {
            "overall_ok": False,
            "items": {
                "duration": {
                    "ok": False,
                    "errors": ["尺不一致(テスト用強制失敗)"],
                }
            },
            "probe_data": {},
        }

    monkeypatch.setattr(run.qa_check, "run_qa", _fake_run_qa)

    report = run.run_pipeline("QA失敗経路テスト", 6.0, "mock", True, cfg)

    assert report["stages"]["qa"]["overall_ok"] is False
    assert report["stages"]["qa"]["ok"] is False
    assert report["ok"] is False

    exit_code = 0 if report.get("ok") else 1
    assert exit_code == 1


# ---------------------------------------------------------------------------
# --style: run.py の CLI引数がdirector.run_directorへ配線されていることの検証
# ---------------------------------------------------------------------------

def test_run_pipeline_passes_style_to_director(monkeypatch):
    """style引数がdirector.run_directorへ届くことを、director呼び出し直後に例外を
    投げて後続ステージ(ffmpeg等)を実行させずに検証する(高速・実生成なし)。"""
    cfg = load_config()
    captured = {}

    def _fake_run_director(theme, config, **kwargs):
        captured["style"] = kwargs.get("style")
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(run.director, "run_director", _fake_run_director)

    report = run.run_pipeline("styleテスト", 6.0, "mock", True, cfg, quality="single", style="vertical_hook")

    assert captured["style"] == "vertical_hook"
    assert report["stages"]["director"]["ok"] is False


def test_run_pipeline_defaults_style_to_default_when_unspecified(monkeypatch):
    cfg = load_config()
    captured = {}

    def _fake_run_director(theme, config, **kwargs):
        captured["style"] = kwargs.get("style")
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(run.director, "run_director", _fake_run_director)

    run.run_pipeline("styleデフォルトテスト", 6.0, "mock", True, cfg, quality="single")

    assert captured["style"] == "default"


def test_main_argparse_style_default_is_default(monkeypatch):
    captured = {}

    def _fake_run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default", **_):
        captured["style"] = style
        return {"run_id": "r1", "stages": {}, "ok": True, "output_path": None, "qa": None}

    monkeypatch.setattr(run, "run_pipeline", _fake_run_pipeline)

    exit_code = run.main(["--theme", "t", "--backend", "mock", "--no-llm"])

    assert exit_code == 0
    assert captured["style"] == "default"


def test_main_argparse_style_vertical_hook_is_forwarded(monkeypatch):
    captured = {}

    def _fake_run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default", **_):
        captured["style"] = style
        return {"run_id": "r1", "stages": {}, "ok": True, "output_path": None, "qa": None}

    monkeypatch.setattr(run, "run_pipeline", _fake_run_pipeline)

    exit_code = run.main(["--theme", "t", "--backend", "mock", "--no-llm", "--style", "vertical_hook"])

    assert exit_code == 0
    assert captured["style"] == "vertical_hook"


def test_main_argparse_style_rejects_invalid_choice():
    with pytest.raises(SystemExit):
        run.main(["--theme", "t", "--style", "not_a_real_style"])


# ---------------------------------------------------------------------------
# 音声主導タイミング同期モード（CLI経路 run.run_pipeline）
# ---------------------------------------------------------------------------

_SYNC_TEST_SHOTS = [
    {
        "id": "s1", "visual_prompt": "abstract a", "motion_preset": "static", "duration_sec": 2.0,
        "caption_jp": "テロップ1", "narration_jp": "断片1です。",
    },
    {
        "id": "s2", "visual_prompt": "abstract b", "motion_preset": "static", "duration_sec": 5.0,
        "caption_jp": "テロップ2", "narration_jp": "断片2です。",
    },
]


def _fake_plan_with_shots(shots, narration_script="断片1です。断片2です。"):
    return {
        "version": 1, "meta": {"source": "ai"}, "concept": "c", "hook": "h",
        "narration_script": narration_script, "shots": shots, "bgm_mood": "none",
    }


@pytest.mark.slow
def test_run_pipeline_segment_sync_mode_computes_display_durations_and_pads_or_trims(monkeypatch):
    """narration_jpが全shotsに揃っていれば同期モードになり、断片実測尺(+0.25秒)で
    ショットごとにtpad(不足分)/truncate(超過分)が使い分けられること。"""
    cfg = load_config()

    monkeypatch.setattr(run.director, "run_director", lambda theme, config, **kw: _fake_plan_with_shots(_SYNC_TEST_SHOTS))

    fake_segments = [
        {"path": "/tmp/seg0.wav", "duration_sec": 3.0},  # display=3.25 > shot1 duration(2.0) -> pad
        {"path": "/tmp/seg1.wav", "duration_sec": 1.0},  # display=1.25 < shot2 duration(5.0) -> truncate
    ]

    def _fake_synthesize_segments(texts, out_dir, cfg_, voice="Kyoko"):
        assert texts == ["断片1です。", "断片2です。"]
        return {"ok": True, "segments": fake_segments, "backend": "fake_seg", "fallback_reason": None}

    monkeypatch.setattr(run.tts_mod, "synthesize_segments", _fake_synthesize_segments)

    calls = []

    def _fake_run_ffmpeg(cmd, timeout_sec=None):
        calls.append(cmd)
        return {"returncode": 0, "stderr": ""}

    monkeypatch.setattr(run.render, "run_ffmpeg", _fake_run_ffmpeg)

    report = run.run_pipeline("同期モードCLIテスト", 7.0, "mock", True, cfg, quality="single", style="default")

    assert report["stages"]["tts"]["mode"] == "segment"
    assert report["stages"]["tts"]["backend"] == "fake_seg"

    # run.pyの実行順序はTTS(Stage4: ナレーション断片配置)が先、正規化(Stage5)が後。
    # calls[0]=ナレーション断片配置concat calls[1]=s1正規化 calls[2]=s2正規化
    assert "adelay=" in " ".join(calls[0])
    assert "tpad=stop_mode=clone:stop_duration=1.250" in " ".join(calls[1])
    assert calls[1][calls[1].index("-t") + 1] == "3.250"
    assert "tpad" not in " ".join(calls[2])
    assert calls[2][calls[2].index("-t") + 1] == "1.250"


@pytest.mark.slow
def test_run_pipeline_missing_narration_jp_on_some_shots_falls_back_to_full_mode(monkeypatch):
    """一部のshotsだけnarration_jpが欠けている場合は同期モードを試みず、従来の全文方式になる。"""
    cfg = load_config()

    shots = [
        _SYNC_TEST_SHOTS[0],
        {"id": "s2", "visual_prompt": "abstract b", "motion_preset": "static", "duration_sec": 2.0, "caption_jp": "テロップ2"},
    ]
    monkeypatch.setattr(
        run.director, "run_director",
        lambda theme, config, **kw: _fake_plan_with_shots(shots, narration_script="全文ナレーションです。"),
    )
    monkeypatch.setattr(
        run.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("narration_jpが欠けているのに呼ばれてはいけない")),
    )

    calls = []
    monkeypatch.setattr(run.render, "run_ffmpeg", lambda cmd, timeout_sec=None: calls.append(cmd) or {"returncode": 0, "stderr": ""})

    called = {}

    class _FakeFullBackend:
        name = "fake_full"

        def synthesize(self, text, out_wav_path, cfg=None):
            called["text"] = text
            Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
            return {"backend": "fake_full", "duration_sec": 4.0, "is_silent": False}

    monkeypatch.setattr(run.tts_mod, "get_tts_backend", lambda voice="Kyoko", cfg=None: _FakeFullBackend())

    report = run.run_pipeline("フォールバックCLIテスト", 4.0, "mock", True, cfg, quality="single", style="default")

    assert report["stages"]["tts"]["mode"] == "full"
    assert called["text"] == "全文ナレーションです。"


# ---------------------------------------------------------------------------
# TTP v2 移行: CLI に --reference-url / --reference-file を追加した経路の検証
# ---------------------------------------------------------------------------

def test_main_rejects_no_reference_when_llm_mode(monkeypatch, capsys):
    """--reference-url / --reference-file が無く --no-llm でもない起動は exit 2 + 案内文。"""
    exit_code = run.main(["--theme", "参考動画無しテスト", "--backend", "mock"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "参考動画URLが指定されていません" in captured.err
    assert "--reference-url" in captured.err
    assert "--no-llm" in captured.err


def test_main_allows_no_llm_without_reference(monkeypatch):
    """--no-llm 起動は reference 無しでも通る（決定論テンプレート経路）。"""
    calls = {}

    def _fake_run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default", **kw):
        calls["no_llm"] = no_llm
        calls["reference_url"] = kw.get("reference_url")
        return {"run_id": "r1", "stages": {}, "ok": True, "output_path": None, "qa": None}

    monkeypatch.setattr(run, "run_pipeline", _fake_run_pipeline)
    exit_code = run.main(["--theme", "no-llm経路", "--backend", "mock", "--no-llm"])
    assert exit_code == 0
    assert calls["no_llm"] is True
    assert calls["reference_url"] is None


def test_run_pipeline_records_reference_stage_with_cached_spec(monkeypatch, tmp_path):
    """--reference-url 指定時の Stage 0 で report.stages.reference に cuts/telops/sfx 件数が
    記録されること。director/tts/render/qa は fake で早期停止して reference段のみを検証する。"""
    cfg = load_config()

    fake_spec = {
        "version": 2, "url": "https://example.com/v/x", "duration_sec": 12.0,
        "cuts": [{"t": 3.0}, {"t": 6.0}],
        "telops": [{"start": 0.0, "end": 2.0, "text": "t"}],
        "sfx_events": [{"t": 3.0, "kind": "impact", "confidence": 0.8}],
        "warnings": [],
    }

    def _fake_analyze(url, cfg=None, fetch_video=None, **kw):
        return {"ok": True, "spec": fake_spec, "source": "cache", "cached": True, "warnings": [], "error": None}

    monkeypatch.setattr(run.reference_v2, "analyze_reference_v2", _fake_analyze)

    def _boom(*a, **kw):
        raise RuntimeError("stop-after-reference")

    monkeypatch.setattr(run.director, "run_director", _boom)

    report = run.run_pipeline(
        "参考ステージテスト", 12.0, "mock", False, cfg, quality="single",
        reference_url="https://example.com/v/x",
    )

    assert report["stages"]["reference"]["ok"] is True
    assert report["stages"]["reference"]["cached"] is True
    assert report["stages"]["reference"]["cuts_count"] == 2
    assert report["stages"]["reference"]["telops_count"] == 1
    assert report["stages"]["reference"]["sfx_count"] == 1
    # 保存されている reference_spec.json が読める
    spec_path = Path(report["run_dir"]) / "reference_spec.json"
    assert spec_path.exists()


def test_run_pipeline_reference_analysis_failure_stops_run(monkeypatch):
    """reference 解析が失敗したら reference stage を error にして run を停止する。"""
    cfg = load_config()

    monkeypatch.setattr(
        run.reference_v2, "analyze_reference_v2",
        lambda url, cfg=None, fetch_video=None, **kw: {
            "ok": False, "spec": None, "error": "取得失敗", "warnings": [],
        },
    )

    def _should_not_be_called(*a, **kw):
        raise AssertionError("参考動画解析失敗後 director が呼ばれてはいけない")

    monkeypatch.setattr(run.director, "run_director", _should_not_be_called)

    report = run.run_pipeline(
        "解析失敗テスト", 12.0, "mock", False, cfg, quality="single",
        reference_url="https://example.com/v/fail",
    )
    assert report["stages"]["reference"]["ok"] is False
    assert "取得失敗" in report["stages"]["reference"]["error"]
    assert report["ok"] is False


def test_load_fish_audio_key_from_secrets(monkeypatch, tmp_path):
    """~/.claude/secrets/fish_audio_key があれば FISH_AUDIO_API_KEY へ載る（既存値は上書きしない）。"""
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    secrets_dir = tmp_path / ".claude" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "fish_audio_key").write_text("dummy-key-value\n", encoding="utf-8")
    monkeypatch.setattr(run.Path, "home", staticmethod(lambda: tmp_path))
    assert run._load_fish_audio_key_from_secrets() is True
    assert run.os.environ["FISH_AUDIO_API_KEY"] == "dummy-key-value"


def test_load_fish_audio_key_preserves_existing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "already-set")
    monkeypatch.setattr(run.Path, "home", staticmethod(lambda: tmp_path))
    assert run._load_fish_audio_key_from_secrets() is True
    assert run.os.environ["FISH_AUDIO_API_KEY"] == "already-set"


# ---------------------------------------------------------------------------
# BUG-55: 表示尺スケールで caption/SFX offsets を線形補正するヘルパ
# ---------------------------------------------------------------------------

def test_scale_shot_for_display_scales_caption_offsets_proportionally():
    """BUG-55: display_duration が duration_sec の 0.5 倍なら caption offsets も 0.5 倍される。"""
    shot = {
        "id": "s1", "duration_sec": 4.0,
        "caption_in_offset_sec": 2.0, "caption_out_offset_sec": 4.0,
    }
    result = run._scale_shot_for_display(shot, 2.0)
    assert result["duration_sec"] == 2.0
    assert result["caption_in_offset_sec"] == 1.0
    assert result["caption_out_offset_sec"] == 2.0


def test_scale_shot_for_display_no_change_when_display_equals_original():
    shot = {"id": "s1", "duration_sec": 3.0, "caption_in_offset_sec": 1.5}
    result = run._scale_shot_for_display(shot, 3.0)
    assert result["caption_in_offset_sec"] == 1.5


def test_scale_shot_for_display_leaves_shot_without_offsets_unchanged():
    shot = {"id": "s1", "duration_sec": 4.0, "caption_jp": "テスト"}
    result = run._scale_shot_for_display(shot, 2.0)
    assert result["duration_sec"] == 2.0
    assert "caption_in_offset_sec" not in result
    assert result["caption_jp"] == "テスト"


def test_build_display_scaled_plan_scales_sfx_plan_offsets():
    """BUG-55: sfx_plan の t_anchor.offset_sec も表示尺スケールで線形補正される。"""
    plan = {
        "shots": [
            {"id": "s1", "duration_sec": 4.0, "caption_in_offset_sec": 2.0,
             "caption_out_offset_sec": 4.0},
            {"id": "s2", "duration_sec": 2.0},
        ],
        "sfx_plan": [
            {"family": "whoosh",
             "t_anchor": {"type": "shot_start", "shot_id": "s1", "offset_sec": 3.0}},
            {"family": "impact",
             "t_anchor": {"type": "shot_start", "shot_id": "s2", "offset_sec": 1.0}},
        ],
    }
    display = {"s1": 2.0, "s2": 2.0}  # s1: 4→2 (0.5倍), s2: 2→2 (等倍)
    scaled = run._build_display_scaled_plan(plan, display)
    assert scaled["shots"][0]["duration_sec"] == 2.0
    assert scaled["shots"][0]["caption_in_offset_sec"] == 1.0
    assert scaled["shots"][0]["caption_out_offset_sec"] == 2.0
    # s1 のSFX offset は 0.5倍
    assert scaled["sfx_plan"][0]["t_anchor"]["offset_sec"] == 1.5
    # s2 は等倍
    assert scaled["sfx_plan"][1]["t_anchor"]["offset_sec"] == 1.0
    # 元 plan は変更されない
    assert plan["shots"][0]["duration_sec"] == 4.0
    assert plan["sfx_plan"][0]["t_anchor"]["offset_sec"] == 3.0


def test_build_display_scaled_plan_handles_missing_sfx_plan():
    plan = {
        "shots": [{"id": "s1", "duration_sec": 3.0}],
    }
    display = {"s1": 2.0}
    scaled = run._build_display_scaled_plan(plan, display)
    assert "sfx_plan" not in scaled
    assert scaled["shots"][0]["duration_sec"] == 2.0
