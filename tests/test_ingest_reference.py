# -*- coding: utf-8 -*-
"""bin/ingest_reference.py の型検証テスト。

bin/ はパッケージ化されていないため importlib でファイルパスから直接ロードする
(project rootに__init__.pyが無くbin.ingest_referenceとしてimportできないため)。
実ネットワーク/実ファイルシステムへの書き込みはcfgのcache_dirをtmp_path配下に固定して行う。
"""
import importlib.util
import json
import os

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INGEST_PATH = os.path.join(_PROJECT_ROOT, "bin", "ingest_reference.py")


def _load_ingest_module():
    spec = importlib.util.spec_from_file_location("ingest_reference_cli", _INGEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingest = _load_ingest_module()


_VALID_RHYTHM = {
    "sentence_count": 5, "avg_sentence_len": 12.0, "max_sentence_len": 24,
    "tone": "カジュアル", "endings": ["〜です", "〜ます"],
}


def _valid_spec():
    return {
        "transcript": "て" * 40,
        "duration_sec": 20.0,
        "beats": [
            {"role": "hook", "start": 0, "end": 5, "text": "hook text", "summary": "s1"},
            {"role": "cta", "start": 5, "end": 20, "text": "cta text", "summary": "s2"},
        ],
        "rhythm": dict(_VALID_RHYTHM),
        "telops": ["ここがポイント"],
    }


def _run_ingest(tmp_path, monkeypatch, spec, url="https://x.com/ingest-test"):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    cache_dir = tmp_path / "cache"

    def fake_load_config():
        return {"reference": {"cache_dir": str(cache_dir)}}

    monkeypatch.setattr(ingest, "load_config", fake_load_config)
    monkeypatch.setattr(
        "sys.argv", ["ingest_reference.py", "--url", url, "--spec-file", str(spec_file)]
    )
    exit_code = ingest.main()
    return exit_code, cache_dir


# ---------------------------------------------------------------------------
# BUG-1(高): 型検証不足 — duration_secが文字列のspecは拒否されるべき
# ---------------------------------------------------------------------------

def test_string_duration_sec_is_rejected(tmp_path, monkeypatch, capsys):
    spec = _valid_spec()
    spec["duration_sec"] = "20秒くらい"

    exit_code, cache_dir = _run_ingest(tmp_path, monkeypatch, spec)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "duration_sec" in out
    assert not cache_dir.exists() or not list(cache_dir.glob("*.json"))


def test_string_duration_sec_does_not_crash_with_traceback(tmp_path, monkeypatch):
    # float("20秒くらい") は ValueError を送出する。これが検証前に呼ばれてクラッシュしない
    # (=main()が例外を投げずcode 1で正常終了する)ことを確認する。
    spec = _valid_spec()
    spec["duration_sec"] = "invalid"
    exit_code, _cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 1


def test_boolean_duration_sec_is_rejected(tmp_path, monkeypatch, capsys):
    spec = _valid_spec()
    spec["duration_sec"] = True

    exit_code, _cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 1
    assert "duration_sec" in capsys.readouterr().out


def test_missing_rhythm_keys_rejected_via_shared_validator(tmp_path, monkeypatch, capsys):
    spec = _valid_spec()
    spec["rhythm"] = {"tone": "x"}

    exit_code, _cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 1
    assert "rhythm" in capsys.readouterr().out


def test_beats_non_numeric_start_end_rejected_via_shared_validator(tmp_path, monkeypatch, capsys):
    spec = _valid_spec()
    spec["beats"][0]["start"] = "zero"

    exit_code, _cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 1
    assert "beats" in capsys.readouterr().out


def test_telops_non_list_rejected(tmp_path, monkeypatch, capsys):
    spec = _valid_spec()
    spec["telops"] = "テロップ文字列"

    exit_code, _cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 1
    assert "telops" in capsys.readouterr().out


def test_telops_null_is_allowed(tmp_path, monkeypatch):
    spec = _valid_spec()
    spec["telops"] = None
    exit_code, cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 0
    assert list(cache_dir.glob("*.json"))


def test_valid_spec_is_written_to_cache(tmp_path, monkeypatch):
    spec = _valid_spec()
    exit_code, cache_dir = _run_ingest(tmp_path, monkeypatch, spec)
    assert exit_code == 0
    files = list(cache_dir.glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["source"] == "vidiq"
    assert written["transcript"] == spec["transcript"]
