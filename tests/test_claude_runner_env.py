# -*- coding: utf-8 -*-
"""pipeline.claude_runner: 入れ子 claude 起動の env 除染と失敗分類のテスト。

実 claude CLI は一切呼ばない（subprocess.Popen を monkeypatch で差し替える）。
本丸の修正 = subprocess へ渡す env から CLAUDECODE / CLAUDE_CODE_* を除去し、
Claude Code サブセッション内で起動されたサーバからでも claude を親セッション扱いで
実行させること。HOME/PATH 等は保持されることを検証する。
"""
import os

from pipeline import claude_runner


def test_clean_subprocess_env_strips_nested_session_markers_only(monkeypatch):
    fake_env = {
        # 除去対象（入れ子セッションマーカー）
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_CODE_EXECPATH": "/x/y",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # 保持対象（認証情報 + 一般環境変数）
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oauth-xxxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxxx",
        "HOME": "/Users/tester",
        "PATH": "/usr/bin:/bin",
        "LANG": "ja_JP.UTF-8",
    }
    monkeypatch.setattr(os, "environ", fake_env)
    cleaned = claude_runner._clean_subprocess_env()

    # 入れ子マーカー（allowlist）は除去される。
    for k in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_EXECPATH",
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        assert k not in cleaned, k

    # ★codex-review P1: CLAUDE_CODE_OAUTH_TOKEN 等の認証系は必ず保持する
    # （prefix除去だと OAuth トークンまで消えて認証成立環境でも claude 呼び出しが落ちる）。
    assert cleaned["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oauth-xxxx"
    assert cleaned["ANTHROPIC_API_KEY"] == "sk-ant-xxxx"
    # HOME / PATH / その他の無関係な変数も保持される。
    assert cleaned["HOME"] == "/Users/tester"
    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["LANG"] == "ja_JP.UTF-8"


def test_clean_subprocess_env_accepts_explicit_base():
    base = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
        "FOO": "bar",
    }
    cleaned = claude_runner._clean_subprocess_env(base)
    # 入れ子マーカーは除去、OAuth と一般変数は保持。
    assert cleaned == {"CLAUDE_CODE_OAUTH_TOKEN": "keep-me", "FOO": "bar"}


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4321

    def communicate(self, timeout=None):
        return self._stdout, self._stderr


def test_run_claude_passes_decontaminated_env(monkeypatch):
    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc(stdout='{"is_error": false, "result": "{}"}', returncode=0)

    monkeypatch.setattr(
        os, "environ",
        {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "HOME": "/h", "PATH": "/p"},
    )
    monkeypatch.setattr(claude_runner.subprocess, "Popen", _fake_popen)

    claude_runner._run_claude("claude", "hello", None, timeout_sec=5)

    env = captured["env"]
    assert env is not None
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_CHILD_SESSION" not in env
    assert env["HOME"] == "/h"
    assert env["PATH"] == "/p"


def test_run_claude_classifies_auth_failure(monkeypatch):
    def _fake_popen(cmd, **kwargs):
        return _FakeProc(stdout="", stderr="Error: Not logged in. Please run claude login", returncode=1)

    monkeypatch.setattr(claude_runner.subprocess, "Popen", _fake_popen)
    try:
        claude_runner._run_claude("claude", "hello", None, timeout_sec=5)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "auth" in str(exc)


def test_run_claude_classifies_not_found(monkeypatch):
    def _fake_popen(cmd, **kwargs):
        return _FakeProc(stdout="", stderr="zsh: command not found: claude", returncode=127)

    monkeypatch.setattr(claude_runner.subprocess, "Popen", _fake_popen)
    try:
        claude_runner._run_claude("claude", "hello", None, timeout_sec=5)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "not_found" in str(exc)


def test_probe_claude_ok(monkeypatch):
    def _fake_run_claude(claude_bin, prompt, model, timeout_sec, **kwargs):
        return '{"is_error": false, "result": "OK", "modelUsage": {"claude-opus-4-8[1m]": {}}}'

    monkeypatch.setattr(claude_runner, "_run_claude", _fake_run_claude)
    res = claude_runner.probe_claude()
    assert res["ok"] is True
    assert res["model_used"] == "claude-opus-4-8"


def test_probe_claude_reports_unreachable(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("claude実行エラー(exit 1/auth): 認証に失敗")

    monkeypatch.setattr(claude_runner, "_run_claude", _boom)
    res = claude_runner.probe_claude()
    assert res["ok"] is False
    assert res["category"] == "unreachable"
