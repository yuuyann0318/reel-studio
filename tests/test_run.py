# -*- coding: utf-8 -*-
"""run.py のstageロジック検証（BUG-3: QA失敗の伝播 / BUG-4: ng_wordsのデフォルト有効化）。"""
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
