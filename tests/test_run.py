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
    assert "みお" in ng_words


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

    def _fake_run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default"):
        captured["style"] = style
        return {"run_id": "r1", "stages": {}, "ok": True, "output_path": None, "qa": None}

    monkeypatch.setattr(run, "run_pipeline", _fake_run_pipeline)

    exit_code = run.main(["--theme", "t", "--backend", "mock", "--no-llm"])

    assert exit_code == 0
    assert captured["style"] == "default"


def test_main_argparse_style_vertical_hook_is_forwarded(monkeypatch):
    captured = {}

    def _fake_run_pipeline(theme, target_duration_sec, backend_name, no_llm, cfg, quality=None, style="default"):
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
