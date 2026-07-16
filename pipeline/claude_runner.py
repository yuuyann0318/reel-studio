# -*- coding: utf-8 -*-
"""claude -p ヘッドレス実行ラッパ（Fable 5「AIディレクター」呼び出し用）。

/Users/yuuya/claude code/video-auto-editor/pipeline/claude_runner.py を移植
（同一パターン: subprocess.Popen / shell=False / start_new_session=True /
SIGTERM→5秒→SIGKILL / エンベロープJSON→result文字列→最初の{〜最後の}救済抽出）。

モデルフォールバックチェーン: ["claude-fable-5", "fable-5", None]（None=--model省略）。
config.claude_model に疎通確定モデルを先頭へ差し込む。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import Optional

from pipeline.config import load_config

_KILL_GRACE_SEC = 5
_MODEL_FALLBACK_CHAIN = ["claude-fable-5", "fable-5", None]


def _kill_tree(proc):
    """SIGTERM→5秒→SIGKILL（プロセスグループごと）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.communicate(timeout=_KILL_GRACE_SEC)
        return
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=_KILL_GRACE_SEC)
    except Exception:
        pass


def _run_claude(claude_bin: str, prompt: str, model: "Optional[str]", timeout_sec: float) -> str:
    cmd = [str(claude_bin), "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", str(model)]
    cmd += ["--max-turns", "1", "--permission-mode", "default", "--tools", ""]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise RuntimeError("claude実行タイムアウト(%s秒超過)" % timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError("claude実行エラー(exit %s): %s" % (proc.returncode, _brief_failure_reason(stdout, stderr)))
    return stdout


def _brief_failure_reason(stdout, stderr):
    try:
        outer = json.loads((stdout or "").strip())
        if isinstance(outer, dict):
            status = outer.get("api_error_status")
            msg = outer.get("result")
            parts = []
            if status is not None:
                parts.append(str(status))
            if isinstance(msg, str) and msg:
                parts.append(msg[:200])
            if parts:
                return ": ".join(parts)
    except Exception:
        pass
    if stderr:
        return stderr[:300]
    if stdout:
        return stdout[:300]
    return "(詳細不明)"


def _extract_inner(stdout_str):
    try:
        outer = json.loads((stdout_str or "").strip())
    except Exception:
        return None, "envelope_json_parse_failed"
    if not isinstance(outer, dict):
        return None, "envelope_not_dict"
    if outer.get("is_error") is True:
        status = outer.get("api_error_status")
        msg = outer.get("result")
        detail = "is_error_true"
        if status is not None:
            detail += "({})".format(status)
        if isinstance(msg, str) and msg:
            detail += ": {}".format(msg[:200])
        return None, detail
    result = outer.get("result")
    if not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False)
        except Exception:
            return None, "result_not_stringifiable"
    first = result.find("{")
    last = result.rfind("}")
    if first == -1 or last <= first:
        return None, "no_json_braces_in_result"
    try:
        return json.loads(result[first:last + 1]), None
    except Exception:
        return None, "inner_json_parse_failed"


def _actual_model_from_envelope(stdout_str, fallback_model):
    """`modelUsage` キー(claude CLI自身の実測)から実際に応答したモデルIDを取得する。

    渡した --model 値やLLMの自己申告は信用しない（実機検証で判明した既知の落とし穴）。
    """
    try:
        outer = json.loads((stdout_str or "").strip())
    except Exception:
        return fallback_model
    model_usage = outer.get("modelUsage") if isinstance(outer, dict) else None
    if isinstance(model_usage, dict) and model_usage:
        raw_key = next(iter(model_usage.keys()))
        bracket = raw_key.find("[")
        return raw_key[:bracket] if bracket != -1 else raw_key
    return fallback_model


def _chain_for(cfg: dict):
    configured = cfg.get("claude_model")
    chain = []
    if configured:
        chain.append(configured)
    for m in _MODEL_FALLBACK_CHAIN:
        if m not in chain:
            chain.append(m)
    return chain


def call_claude_json(prompt: str, timeout_sec: int = 600, model_override: "Optional[str]" = None) -> dict:
    """claude -p を実行し、内側JSONを返す。例外は投げない。

    Returns:
        {"ok": bool, "data": dict|None, "raw": str, "model_used": str|None,
         "error": str|None, "attempts": list,
         "fallback_from": str|None, "fallback_reason": str|None}
    """
    cfg = load_config()
    claude_bin = cfg.get("claude_bin", "claude")
    chain = [model_override] if model_override else _chain_for(cfg)
    primary = chain[0] if chain else None

    attempts = []
    last_raw = ""
    for model in chain:
        try:
            stdout = _run_claude(claude_bin, prompt, model, timeout_sec)
        except Exception as exc:
            attempts.append({"model": model, "ok": False, "error": str(exc)[:300]})
            continue
        last_raw = stdout
        inner, err = _extract_inner(stdout)
        if inner is not None:
            attempts.append({"model": model, "ok": True})
            actual_model = _actual_model_from_envelope(stdout, model)
            fallback_from = None
            fallback_reason = None
            if model != primary and primary is not None:
                prior_failure = next(
                    (a for a in reversed(attempts) if a["model"] == primary and not a["ok"]), None
                )
                fallback_from = primary
                fallback_reason = prior_failure["error"] if prior_failure else "unknown"
            return {
                "ok": True,
                "data": inner,
                "raw": stdout,
                "model_used": actual_model,
                "error": None,
                "attempts": attempts,
                "fallback_from": fallback_from,
                "fallback_reason": fallback_reason,
            }
        attempts.append({"model": model, "ok": False, "error": err})

    return {
        "ok": False,
        "data": None,
        "raw": last_raw,
        "model_used": None,
        "error": "all_models_failed",
        "attempts": attempts,
        "fallback_from": None,
        "fallback_reason": None,
    }
