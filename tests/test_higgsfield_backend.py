# -*- coding: utf-8 -*-
"""higgsfield_backend の単体テスト（subprocessをモック・実CLI呼び出しなし）。"""
import subprocess

import pytest

from pipeline.visual import higgsfield_backend as hb
from pipeline.visual.base import VisualBackendError


def _shot(**overrides):
    base = {
        "id": "s1",
        "visual_prompt": "calm ocean horizon at sunrise, no people, no text",
        "motion_preset": "static",
        "duration_sec": 5,
        "caption_jp": "テスト",
    }
    base.update(overrides)
    return base


def _cfg(**hf_overrides):
    hf = {
        "cli_bin": "higgsfield",
        "model": "seedance_2_0_mini",
        "resolution": "480p",
        "max_credits_per_shot": 10,
        "poll_interval_sec": 1,
        "poll_timeout_sec": 30,
    }
    hf.update(hf_overrides)
    return {"higgsfield": hf}


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- コマンド構築（純関数） -------------------------------------------------

def test_build_create_cmd_has_expected_flags():
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", _shot(), "480p")
    assert cmd[:4] == ["higgsfield", "generate", "create", "seedance_2_0_mini"]
    assert "--prompt" in cmd
    assert "--aspect-ratio" in cmd and "9:16" in cmd
    assert "--resolution" in cmd and "480p" in cmd
    assert "--duration" in cmd and "5" in cmd
    assert "--json" in cmd


def test_build_create_cmd_rounds_float_duration_to_int():
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", _shot(duration_sec=4.6), "480p")
    idx = cmd.index("--duration")
    assert cmd[idx + 1] == "5"


def test_build_cost_cmd_has_expected_flags():
    cmd = hb._build_cost_cmd("higgsfield", "seedance_2_0_mini", _shot(), "480p")
    assert cmd[:4] == ["higgsfield", "generate", "cost", "seedance_2_0_mini"]
    assert "--duration" in cmd and "5" in cmd


def test_build_wait_cmd_has_duration_style_timeout_interval():
    cmd = hb._build_wait_cmd("higgsfield", "job-123", timeout_sec=600, interval_sec=5)
    assert cmd == ["higgsfield", "generate", "wait", "job-123", "--timeout", "600s", "--interval", "5s", "--json"]


# --- レスポンスパース --------------------------------------------------------

def test_parse_job_id_from_json_array():
    assert hb._parse_job_id('["2296cac2-ed3c-460f-a2ed-b1050047f0e5"]') == "2296cac2-ed3c-460f-a2ed-b1050047f0e5"


def test_parse_job_id_from_json_object_fallback():
    assert hb._parse_job_id('{"id": "abc-123"}') == "abc-123"


def test_parse_job_id_raises_on_empty_array():
    with pytest.raises(VisualBackendError):
        hb._parse_job_id("[]")


def test_parse_job_id_raises_on_invalid_json():
    with pytest.raises(VisualBackendError):
        hb._parse_job_id("not json")


def test_parse_cost_extracts_credits_int():
    assert hb._parse_cost('{"credits": 5}') == 5


def test_parse_cost_raises_when_missing():
    with pytest.raises(VisualBackendError):
        hb._parse_cost('{"other": 1}')


def test_parse_wait_result_completed_maps_status_and_result_url():
    stdout = '{"status": "completed", "result_url": "https://example.com/a.mp4"}'
    result = hb._parse_wait_result(stdout)
    assert result["status"] == "completed"
    assert result["result_url"] == "https://example.com/a.mp4"


def test_parse_wait_result_failed_status():
    stdout = '{"status": "failed", "error": "content policy violation"}'
    result = hb._parse_wait_result(stdout)
    assert result["status"] == "failed"
    assert result["error"] == "content policy violation"


def test_parse_wait_result_raises_on_invalid_json():
    with pytest.raises(VisualBackendError):
        hb._parse_wait_result("not json")


# --- 認証エラー / タイムアウトの分類 -----------------------------------------

def test_run_cli_raises_auth_error_when_stderr_says_not_authenticated(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        return _FakeProc(returncode=1, stderr=b"Error: Not authenticated. Run `higgsfield auth login`.")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(hb.HiggsfieldAuthError):
        hb._run_cli(["higgsfield", "generate", "create", "m", "--json"], timeout_sec=10)


def test_run_cli_raises_timeout_error_when_python_subprocess_times_out(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(hb.HiggsfieldTimeoutError):
        hb._run_cli(["higgsfield", "generate", "wait", "job-1", "--json"], timeout_sec=10)


def test_run_cli_raises_timeout_error_when_cli_reports_timeout_in_stderr(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        return _FakeProc(returncode=1, stderr=b"Error: wait timed out after 600s")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(hb.HiggsfieldTimeoutError):
        hb._run_cli(["higgsfield", "generate", "wait", "job-1", "--json"], timeout_sec=700)


def test_run_cli_raises_generic_error_for_other_failures(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        return _FakeProc(returncode=1, stderr=b"Error: model not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(VisualBackendError) as excinfo:
        hb._run_cli(["higgsfield", "generate", "create", "bad_model", "--json"], timeout_sec=10)
    assert not isinstance(excinfo.value, hb.HiggsfieldAuthError)
    assert not isinstance(excinfo.value, hb.HiggsfieldTimeoutError)


def test_run_cli_file_not_found_gives_install_hint(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(VisualBackendError) as excinfo:
        hb._run_cli(["higgsfield", "generate", "create", "m", "--json"], timeout_sec=10)
    assert "npm i -g @higgsfield/cli" in str(excinfo.value)


# --- コスト超過中断（課金安全弁） --------------------------------------------

def test_generate_aborts_without_submitting_job_when_cost_exceeds_limit(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg(max_credits_per_shot=3))

    monkeypatch.setattr(backend, "estimate_cost", lambda shot: 5)

    def _fail_submit(shot):
        raise AssertionError("cost超過なのにジョブを投入してはいけない")

    monkeypatch.setattr(backend, "submit_job", _fail_submit)

    with pytest.raises(hb.HiggsfieldCostLimitError):
        backend.generate(_shot(), "/tmp/out.mp4")


def test_generate_proceeds_when_cost_within_limit(monkeypatch, tmp_path):
    backend = hb.HiggsfieldBackend(_cfg(max_credits_per_shot=10))
    out_path = str(tmp_path / "out.mp4")

    monkeypatch.setattr(backend, "estimate_cost", lambda shot: 5)
    monkeypatch.setattr(backend, "submit_job", lambda shot: "job-1")
    monkeypatch.setattr(
        backend, "wait_for_result",
        lambda job_id: {"status": "completed", "result_url": "https://example.com/a.mp4", "error": None},
    )
    downloaded = {}

    def _fake_download(job_id, path, result_url=None):
        downloaded["job_id"] = job_id
        downloaded["path"] = path
        downloaded["result_url"] = result_url

    monkeypatch.setattr(backend, "fetch_result", _fake_download)

    meta = backend.generate(_shot(), out_path)
    assert meta["backend"] == "higgsfield"
    assert meta["job_id"] == "job-1"
    assert meta["credits_estimated"] == 5
    assert meta["result_url"] == "https://example.com/a.mp4"
    assert downloaded["job_id"] == "job-1"
    assert downloaded["path"] == out_path


def test_generate_raises_job_failed_error_on_failed_status(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    monkeypatch.setattr(backend, "estimate_cost", lambda shot: 5)
    monkeypatch.setattr(backend, "submit_job", lambda shot: "job-1")
    monkeypatch.setattr(
        backend, "wait_for_result",
        lambda job_id: {"status": "failed", "result_url": None, "error": "content policy violation"},
    )
    with pytest.raises(hb.HiggsfieldJobFailedError):
        backend.generate(_shot(), "/tmp/out.mp4")


def test_generate_raises_timeout_error_on_non_completed_non_failed_status(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    monkeypatch.setattr(backend, "estimate_cost", lambda shot: 5)
    monkeypatch.setattr(backend, "submit_job", lambda shot: "job-1")
    monkeypatch.setattr(
        backend, "wait_for_result",
        lambda job_id: {"status": "running", "result_url": None, "error": None},
    )
    with pytest.raises(hb.HiggsfieldTimeoutError):
        backend.generate(_shot(), "/tmp/out.mp4")


# --- CLIバイナリパス解決 -----------------------------------------------------

def test_resolve_cli_bin_prefers_which_result(monkeypatch):
    monkeypatch.setattr(hb.shutil, "which", lambda name: "/usr/local/bin/higgsfield")
    assert hb._resolve_cli_bin("higgsfield") == "/usr/local/bin/higgsfield"


def test_resolve_cli_bin_falls_back_to_node_bin_when_which_fails(monkeypatch):
    monkeypatch.setattr(hb.shutil, "which", lambda name: None)
    monkeypatch.setattr(hb.os.path, "exists", lambda p: p == hb._NODE_BIN_FALLBACK)
    assert hb._resolve_cli_bin("higgsfield") == hb._NODE_BIN_FALLBACK


def test_resolve_cli_bin_uses_absolute_path_if_it_exists(monkeypatch):
    monkeypatch.setattr(hb.os.path, "exists", lambda p: p == "/opt/custom/higgsfield")
    assert hb._resolve_cli_bin("/opt/custom/higgsfield") == "/opt/custom/higgsfield"


# --- fetch_result の result_url 必須チェック ---------------------------------

def test_fetch_result_raises_when_no_result_url():
    backend = hb.HiggsfieldBackend(_cfg())
    with pytest.raises(VisualBackendError):
        backend.fetch_result("job-1", "/tmp/out.mp4", result_url=None)
