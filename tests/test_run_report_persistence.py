# -*- coding: utf-8 -*-
"""run._write_report が output/reports/<run_id>.json も書き出すことの単体テスト。

output/report.json 単一だと連続 run で最新1本しか残らず、過去 run のトレースが失われる。
run_id ごとの永続保存を追加した（既存 output/report.json は互換で維持）。
"""
import json
from pathlib import Path

import run as run_mod


def test_write_report_persists_per_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "project_root", lambda: tmp_path)
    report = {
        "run_id": "20260721-999999-abcdef-test",
        "theme": "テスト",
        "stages": {"director": {"ok": True}},
        "ok": True,
    }
    run_mod._write_report(report)
    latest = tmp_path / "output" / "report.json"
    persisted = tmp_path / "output" / "reports" / "20260721-999999-abcdef-test.json"
    assert latest.exists()
    assert persisted.exists()
    a = json.loads(latest.read_text(encoding="utf-8"))
    b = json.loads(persisted.read_text(encoding="utf-8"))
    assert a == b == report


def test_write_report_still_writes_report_json_without_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "project_root", lambda: tmp_path)
    report = {"theme": "no run_id", "stages": {}, "ok": False}
    run_mod._write_report(report)
    latest = tmp_path / "output" / "report.json"
    assert latest.exists()
    # reports/ は run_id が無ければ書かない
    reports_dir = tmp_path / "output" / "reports"
    if reports_dir.exists():
        assert list(reports_dir.iterdir()) == []
