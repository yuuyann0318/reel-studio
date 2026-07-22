# -*- coding: utf-8 -*-
"""claude -p ヘッドレス実行ラッパ（Fable 5「AIディレクター」呼び出し用）。

video-auto-editor/pipeline/claude_runner.py を移植
（同一パターン: subprocess.Popen / shell=False / start_new_session=True /
SIGTERM→5秒→SIGKILL / エンベロープJSON→result文字列→最初の{〜最後の}救済抽出）。

モデルフォールバックチェーン: ["claude-opus-4-8", "claude-fable-5", None]（None=--model省略）。
config.claude_model に疎通確定モデルを先頭へ差し込む。
2026-07-17実機確認: `claude --model claude-opus-4-8` は受理され、応答エンベロープの
modelUsage キーも "claude-opus-4-8" になることを確認済み（自己申告ではなく実測）。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import Optional

from pipeline.config import load_config

_KILL_GRACE_SEC = 5
_MODEL_FALLBACK_CHAIN = ["claude-opus-4-8", "claude-fable-5", None]


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


def _run_claude(
    claude_bin: str,
    prompt: str,
    model: "Optional[str]",
    timeout_sec: float,
    allowed_tools: "Optional[str]" = None,
    add_dirs: "Optional[list]" = None,
    max_turns: int = 1,
) -> str:
    cmd = [str(claude_bin), "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", str(model)]
    if allowed_tools is not None:
        # Read等をvisionのために許可する場合はここに渡す。既定(None)は従来のtools=""を維持。
        cmd += ["--max-turns", str(max_turns), "--permission-mode", "default", "--allowedTools", str(allowed_tools)]
    else:
        cmd += ["--max-turns", str(max_turns), "--permission-mode", "default", "--tools", ""]
    for d in (add_dirs or []):
        cmd += ["--add-dir", str(d)]
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
    # 空文字/未設定は PATH 解決(`claude` コマンド名)に委ねる。
    claude_bin = cfg.get("claude_bin") or "claude"
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


def call_claude_vision_json(
    prompt: str,
    image_paths: list,
    timeout_sec: int = 600,
    model_override: "Optional[str]" = None,
) -> dict:
    """claude -p に Read ツール権限を付けて画像フレームを解析させ、内側JSON(配列 or dict)を返す。

    docs/TTP_V2_MIGRATION.md の vision スパイクで確認済みの構成
    (--allowedTools "Read" + --output-format json + 絶対パス埋め込み) を踏襲する。
    画像パスは絶対パスで prompt に埋め込む前提。呼び出し側で1回のcallに複数画像をバッチする。
    """
    cfg = load_config()
    # 空文字/未設定は PATH 解決(`claude` コマンド名)に委ねる。
    claude_bin = cfg.get("claude_bin") or "claude"
    chain = [model_override] if model_override else _chain_for(cfg)
    primary = chain[0] if chain else None

    add_dirs = []
    seen = set()
    for p in (image_paths or []):
        if not p:
            continue
        d = os.path.dirname(os.path.abspath(str(p)))
        if d and d not in seen:
            seen.add(d)
            add_dirs.append(d)

    attempts = []
    last_raw = ""
    for model in chain:
        try:
            stdout = _run_claude(
                claude_bin,
                prompt,
                model,
                timeout_sec,
                allowed_tools="Read",
                add_dirs=add_dirs,
                max_turns=max(2, len(image_paths or []) + 1),
            )
        except Exception as exc:
            attempts.append({"model": model, "ok": False, "error": str(exc)[:300]})
            continue
        last_raw = stdout
        inner, err = _extract_inner_array_or_dict(stdout)
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


def _extract_inner_array_or_dict(stdout_str):
    """visionプロンプトはJSON配列を返す場合とオブジェクトを返す場合の両対応。"""
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
    # まず配列を優先探索
    fb = result.find("[")
    lb = result.rfind("]")
    fo = result.find("{")
    lo = result.rfind("}")
    candidates = []
    if fb != -1 and lb > fb:
        candidates.append(("array", result[fb:lb + 1]))
    if fo != -1 and lo > fo:
        candidates.append(("object", result[fo:lo + 1]))
    # 開始位置が早い方（応答本体である可能性が高い）から試す
    candidates.sort(key=lambda x: result.find(x[1][0]))
    for _kind, blob in candidates:
        try:
            return json.loads(blob), None
        except Exception:
            continue
    return None, "no_valid_json_in_result"
