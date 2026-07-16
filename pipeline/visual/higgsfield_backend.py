# -*- coding: utf-8 -*-
"""Higgsfield CLI バックエンド（未検証・要 `higgsfield --help` 確認）。

★重要: Higgsfield CLI（`npm i -g @higgsfield/cli` でインストールされる想定の
`higgsfield` コマンド）の実際のサブコマンド名・引数・ジョブID/ポーリングの
JSON構造は、本実装時点(2026-07-17)で未確認。以下は「一般的なtext-to-video系
CLIツールの典型的なインターフェース」を仮定したプレースホルダであり、
実際に使う前に必ず `higgsfield --help` / `higgsfield generate --help` 等を
実行してサブコマンド名・フラグ名・出力JSON形式を確認し、_build_generate_cmd() /
_parse_job_id() / _parse_status() の3関数だけを実際のCLI仕様に合わせて
修正すること（コマンド構築をこの3関数に隔離してあるので、他のコードは
変更不要な設計にしてある）。

CLI未インストール時は、シェルの `FileNotFoundError` を捕捉して
「higgsfield CLIが見つかりません。`npm i -g @higgsfield/cli && higgsfield auth login`
を実行してください」という明確なエラーメッセージに変換する。
"""
from __future__ import annotations

import json
import subprocess

from pipeline.config import load_config
from pipeline.visual.base import VisualBackend, VisualBackendError


def _build_generate_cmd(cli_bin, shot):
    """text-to-video 生成ジョブを投げるコマンドを構築する（副作用なし・テスト対象）。

    ★要確認: 実際のサブコマンドは `higgsfield generate video` かもしれないし
    `higgsfield video create` かもしれない。現時点では未確認のため、最も一般的な
    想定として `higgsfield generate --prompt ... --aspect-ratio ... --duration ... --json`
    という形を仮に置いている。
    """
    return [
        cli_bin, "generate",
        "--prompt", shot.get("visual_prompt", ""),
        "--aspect-ratio", "9:16",
        "--duration", str(shot.get("duration_sec", 5)),
        "--json",
    ]


def _build_status_cmd(cli_bin, job_id):
    """★要確認: ジョブ状態取得の実サブコマンド名。"""
    return [cli_bin, "status", job_id, "--json"]


def _build_download_cmd(cli_bin, job_id, out_path):
    """★要確認: 成果物ダウンロードの実サブコマンド名。"""
    return [cli_bin, "download", job_id, "--output", out_path]


def _parse_job_id(stdout_str):
    """generateコマンドの標準出力(JSON想定)からjob_idを取り出す。★要確認: 実際のキー名。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError("higgsfield generate の応答JSONをパースできませんでした: {!r}".format(stdout_str[:300]))
    job_id = data.get("job_id") or data.get("id")
    if not job_id:
        raise VisualBackendError("higgsfield generate の応答にjob_id/idが見つかりません: {!r}".format(data))
    return job_id


def _parse_status(stdout_str):
    """statusコマンドの標準出力(JSON想定)を {"status","error"} へ正規化する。★要確認: 実際の値の綴り。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError("higgsfield status の応答JSONをパースできませんでした: {!r}".format(stdout_str[:300]))
    raw_status = (data.get("status") or "").lower()
    if raw_status in ("done", "completed", "succeeded", "success"):
        status = "done"
    elif raw_status in ("failed", "error"):
        status = "failed"
    else:
        status = "running"
    return {"status": status, "error": data.get("error")}


def _run_cli(cmd, timeout_sec):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout_sec)
    except FileNotFoundError:
        raise VisualBackendError(
            "higgsfield CLIが見つかりません。`npm i -g @higgsfield/cli && higgsfield auth login` を実行してください。"
        )
    except subprocess.TimeoutExpired:
        raise VisualBackendError("higgsfield CLI実行がタイムアウトしました: {}".format(" ".join(cmd)))
    if proc.returncode != 0:
        raise VisualBackendError(
            "higgsfield CLI実行エラー(exit {}): {}".format(
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:500]
            )
        )
    return proc.stdout.decode("utf-8", "replace")


class HiggsfieldBackend(VisualBackend):
    name = "higgsfield"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        full_cfg = cfg or load_config()
        self.hf_cfg = full_cfg.get("higgsfield", {}) or {}
        self.cli_bin = self.hf_cfg.get("cli_bin", "higgsfield")
        self.poll_interval_sec = self.hf_cfg.get("poll_interval_sec", 5)
        self.poll_timeout_sec = self.hf_cfg.get("poll_timeout_sec", 600)

    def submit_job(self, shot: dict) -> str:
        cmd = _build_generate_cmd(self.cli_bin, shot)
        stdout = _run_cli(cmd, timeout_sec=60)
        return _parse_job_id(stdout)

    def poll_job(self, job_id: str) -> dict:
        cmd = _build_status_cmd(self.cli_bin, job_id)
        stdout = _run_cli(cmd, timeout_sec=30)
        return _parse_status(stdout)

    def fetch_result(self, job_id: str, out_path: str) -> None:
        cmd = _build_download_cmd(self.cli_bin, job_id, out_path)
        _run_cli(cmd, timeout_sec=120)

    def generate(self, shot: dict, out_path: str) -> dict:
        return self.run_async_job(
            shot, out_path, poll_interval_sec=self.poll_interval_sec, poll_timeout_sec=self.poll_timeout_sec
        )
