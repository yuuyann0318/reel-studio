# -*- coding: utf-8 -*-
"""Higgsfield CLI バックエンド（実CLI仕様確認済み・2026-07-17）。

実機で確認したCLI仕様（`higgsfield --help` 系を実行して確認済み。バージョン
`higgsfield 1.1.17`）:

  - 投入: `higgsfield generate create <job_type> --prompt "..." --aspect-ratio 9:16
    --resolution 480p|720p --duration <int秒> --json`
    → stdoutはジョブIDの JSON配列（例 `["2296cac2-...-...-..."]`）。
  - 完了待ち: `higgsfield generate wait <job_id> --timeout <dur> --interval <dur> --json`
    → JSONオブジェクト。`status` が `"completed"` で `result_url` にmp4のURL。
    `status` が `"failed"`/`"error"` ならエラー。CLI自身の `--timeout` 超過時は
    非ゼロ終了 + stderrにタイムアウト文言（保険としてPython側subprocessにも
    余裕を持たせたタイムアウトを設定する）。
  - コスト見積: `higgsfield generate cost <job_type> --prompt "..." --aspect-ratio 9:16
    --resolution 480p --duration 5 --json` → `{"credits": <int>}`。
  - 未認証時: stderrに "Not authenticated"（大小文字は揺れうる）を含む。
  - モデル(seedance_2_0_mini)側のduration制約（BUG-10・実機確認）: `--duration` に
    3秒等の短い値を渡すと `duration: Input should be greater than or equal to 4` という
    バリデーションエラー(exit code 3)を返しジョブが投入されない。最小4秒が必要
    （`_resolve_request_duration_sec()` でクランプして対処）。

コマンド構築・レスポンス解釈は `_build_*_cmd()` / `_parse_*()` 関数に隔離してあり、
CLI側の仕様変更が起きてもこれらの関数だけ直せば追従できる設計にしてある。

CLI未インストール時は `FileNotFoundError` を捕捉して明確なエラーメッセージに変換する。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess

from pipeline.config import load_config
from pipeline.visual.base import VisualBackend, VisualBackendError

# `higgsfield` バイナリが PATH に無い環境向けの明示フォールバック（node同梱パス）。
_NODE_BIN_FALLBACK = (
    "/Users/yuuya/claude code/node-v22.11.0-darwin-arm64/bin/higgsfield"
)

_DEFAULT_MODEL = "seedance_2_0_mini"
_DEFAULT_RESOLUTION = "480p"
_DEFAULT_MAX_CREDITS_PER_SHOT = 10

# ★BUG-10（実機確認・2026-07-17, higgsfield CLI 1.1.17, model=seedance_2_0_mini）:
# directorが3秒ショットを計画し、そのduration_secをそのまま `--duration` に渡したところ、
# CLIが `duration: Input should be greater than or equal to 4` というバリデーションエラー
# (exit code 3) を返し、ジョブが1件も投入されずフルリール生成が失敗した。
# モデル側の最小duration制約が4秒であることを実機エラーで確認したため定数化する。
_MIN_REQUEST_DURATION_SEC = 4
# 上限側は実機のエラーで確認したものではない（未検証）。暴走的なコスト増加・過大な
# クレジット消費を防ぐための保守的なキャップとして設定する。実際の計画尺より長く
# リクエストした分は、後段の render.build_normalize_clip_cmd（duration_sec指定）で
# 計画どおりの尺にトリムして吸収する。
_MAX_REQUEST_DURATION_SEC = 12


def _resolve_request_duration_sec(shot):
    """ショット尺(shot['duration_sec'])から、CLIへ実際にリクエストする秒数を決める。

    副作用なし・テスト対象。ショット尺をそのままモデルへ渡すと、モデルの最小/最大
    duration制約に反してCLIがバリデーションエラーで失敗することがある(BUG-10)。
    ceil(ショット尺) を [_MIN_REQUEST_DURATION_SEC, _MAX_REQUEST_DURATION_SEC] にクランプ
    して安全な値でリクエストし、実際の最終カット尺は plan.shots[].duration_sec を正として
    後段のトリム処理に委ねる。
    """
    duration_sec = float(shot.get("duration_sec", 5))
    requested = int(math.ceil(duration_sec))
    return max(_MIN_REQUEST_DURATION_SEC, min(_MAX_REQUEST_DURATION_SEC, requested))


class HiggsfieldAuthError(VisualBackendError):
    """`higgsfield auth login` が必要（認証切れ/未認証）な場合。"""


class HiggsfieldTimeoutError(VisualBackendError):
    """ジョブ待機がタイムアウトした場合。"""


class HiggsfieldJobFailedError(VisualBackendError):
    """ジョブがCLI/サーバ側で失敗ステータスになった場合。"""


class HiggsfieldCostLimitError(VisualBackendError):
    """見積コストが max_credits_per_shot を超えるため、ジョブを投入せず中断した場合。"""


def _resolve_cli_bin(configured):
    """設定値からCLIの実行パスを解決する。

    絶対パスで実在すればそのまま使う。PATH解決可能ならそれを使う。
    どちらも失敗したら node 同梱パスへ明示フォールバックする（それも無ければ
    設定値をそのまま返し、実行時の FileNotFoundError で明確なエラーにする）。
    """
    configured = configured or "higgsfield"
    if os.path.isabs(configured) and os.path.exists(configured):
        return configured
    found = shutil.which(configured)
    if found:
        return found
    if os.path.exists(_NODE_BIN_FALLBACK):
        return _NODE_BIN_FALLBACK
    return configured


def _build_create_cmd(cli_bin, model, shot, resolution):
    """ジョブ投入コマンドを構築する（副作用なし・テスト対象）。

    ★generate_audio 等の任意パラメータはCLI側デフォルト（generate_audio=true）に
    委ね、明示的なフラグ指定はしない。フラグ名のkebab/snake表記ゆれを実機未検証の
    まま組み込むリスクを避けるため（--prompt/--aspect-ratio/--resolution/--duration
    の4つのみ実機確認済み)。
    """
    duration_sec = _resolve_request_duration_sec(shot)
    return [
        cli_bin, "generate", "create", model,
        "--prompt", shot.get("visual_prompt", ""),
        "--aspect-ratio", "9:16",
        "--resolution", resolution,
        "--duration", str(duration_sec),
        "--json",
    ]


def _build_cost_cmd(cli_bin, model, shot, resolution):
    """コスト見積コマンドを構築する（副作用なし・テスト対象）。"""
    duration_sec = _resolve_request_duration_sec(shot)
    return [
        cli_bin, "generate", "cost", model,
        "--prompt", shot.get("visual_prompt", ""),
        "--aspect-ratio", "9:16",
        "--resolution", resolution,
        "--duration", str(duration_sec),
        "--json",
    ]


def _build_wait_cmd(cli_bin, job_id, timeout_sec, interval_sec):
    """完了待ちコマンドを構築する（副作用なし・テスト対象）。CLIの --timeout/--interval は Go の duration文字列（例 "600s"）。"""
    return [
        cli_bin, "generate", "wait", job_id,
        "--timeout", "{}s".format(int(timeout_sec)),
        "--interval", "{}s".format(int(interval_sec)),
        "--json",
    ]


def _parse_job_id(stdout_str):
    """createコマンドの標準出力(JSON配列想定)からjob_idを取り出す。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate create の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    if isinstance(data, list):
        if not data:
            raise VisualBackendError("higgsfield generate create の応答が空配列でした: {!r}".format(data))
        job_id = data[0]
    elif isinstance(data, dict):
        job_id = data.get("id") or data.get("job_id")
    else:
        job_id = None
    if not job_id:
        raise VisualBackendError("higgsfield generate create の応答からjob_idが取得できません: {!r}".format(data))
    return job_id


def _parse_cost(stdout_str):
    """costコマンドの標準出力(JSON想定)からcredits(int)を取り出す。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate cost の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    credits = data.get("credits") if isinstance(data, dict) else None
    if credits is None:
        raise VisualBackendError("higgsfield generate cost の応答からcreditsが取得できません: {!r}".format(data))
    return int(credits)


def _parse_wait_result(stdout_str):
    """waitコマンドの標準出力(JSONオブジェクト想定)を正規化する。

    Returns: {"status": "completed"|"failed", "result_url": str|None, "error": str|None}
    """
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate wait の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    raw_status = (data.get("status") or "").lower()
    if raw_status in ("completed", "done", "succeeded", "success"):
        status = "completed"
    elif raw_status in ("failed", "error"):
        status = "failed"
    else:
        status = raw_status or "unknown"
    return {
        "status": status,
        "result_url": data.get("result_url"),
        "error": data.get("error"),
        "raw": data,
    }


def _is_auth_error(stderr_text):
    return "not authenticated" in (stderr_text or "").lower()


def _is_timeout_error(stderr_text):
    lowered = (stderr_text or "").lower()
    return "timed out" in lowered or "timeout" in lowered


def _run_cli(cmd, timeout_sec):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout_sec)
    except FileNotFoundError:
        raise VisualBackendError(
            "higgsfield CLIが見つかりません。`npm i -g @higgsfield/cli && higgsfield auth login` を実行してください。"
        )
    except subprocess.TimeoutExpired:
        raise HiggsfieldTimeoutError(
            "higgsfield CLI実行がタイムアウトしました(Python側{}秒超過): {}".format(timeout_sec, " ".join(cmd))
        )
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", "replace")
        if _is_auth_error(stderr_text):
            raise HiggsfieldAuthError(
                "higgsfieldの認証が切れています。`higgsfield auth login` を実行してください。stderr: {}".format(
                    stderr_text[:300]
                )
            )
        if _is_timeout_error(stderr_text):
            raise HiggsfieldTimeoutError(
                "higgsfield CLIがタイムアウトを報告しました: {}".format(stderr_text[:500])
            )
        raise VisualBackendError(
            "higgsfield CLI実行エラー(exit {}): {}".format(proc.returncode, stderr_text[:500])
        )
    return proc.stdout.decode("utf-8", "replace")


def _download_url(url, out_path, curl_bin=None, timeout_sec=120):
    """result_urlをout_pathへcurlでダウンロードする（副作用あり）。"""
    curl_bin = curl_bin or shutil.which("curl") or "/usr/bin/curl"
    cmd = [curl_bin, "-fsSL", "-o", out_path, url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout_sec)
    except FileNotFoundError:
        raise VisualBackendError("curlコマンドが見つかりません。result_urlのダウンロードができません。")
    except subprocess.TimeoutExpired:
        raise VisualBackendError("result_urlのダウンロードがタイムアウトしました: {}".format(url))
    if proc.returncode != 0:
        raise VisualBackendError(
            "result_urlのダウンロードに失敗しました(exit {}): {}".format(
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:500]
            )
        )


class HiggsfieldBackend(VisualBackend):
    name = "higgsfield"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        full_cfg = cfg or load_config()
        self.hf_cfg = full_cfg.get("higgsfield", {}) or {}
        self.cli_bin = _resolve_cli_bin(self.hf_cfg.get("cli_bin", "higgsfield"))
        self.model = self.hf_cfg.get("model", _DEFAULT_MODEL)
        self.resolution = self.hf_cfg.get("resolution", _DEFAULT_RESOLUTION)
        self.max_credits_per_shot = self.hf_cfg.get("max_credits_per_shot", _DEFAULT_MAX_CREDITS_PER_SHOT)
        self.poll_interval_sec = self.hf_cfg.get("poll_interval_sec", 5)
        self.poll_timeout_sec = self.hf_cfg.get("poll_timeout_sec", 600)

    # -- 個別ステップ（テスト容易性のため分割） -----------------------------

    def estimate_cost(self, shot: dict) -> int:
        cmd = _build_cost_cmd(self.cli_bin, self.model, shot, self.resolution)
        stdout = _run_cli(cmd, timeout_sec=60)
        return _parse_cost(stdout)

    def submit_job(self, shot: dict) -> str:
        cmd = _build_create_cmd(self.cli_bin, self.model, shot, self.resolution)
        stdout = _run_cli(cmd, timeout_sec=60)
        return _parse_job_id(stdout)

    def wait_for_result(self, job_id: str) -> dict:
        cmd = _build_wait_cmd(self.cli_bin, job_id, self.poll_timeout_sec, self.poll_interval_sec)
        # Python側subprocessタイムアウトはCLI自身の--timeoutより余裕を持たせる（CLI側の
        # タイムアウト処理を正としつつ、CLIが応答不能になった場合の保険として使う）。
        stdout = _run_cli(cmd, timeout_sec=self.poll_timeout_sec + 60)
        return _parse_wait_result(stdout)

    def fetch_result(self, job_id: str, out_path: str, result_url=None) -> None:
        if not result_url:
            raise VisualBackendError(
                "higgsfield: result_urlが無いためダウンロードできません(job_id={})".format(job_id)
            )
        _download_url(result_url, out_path)

    # ★注意: 基底クラス VisualBackend.poll_job()/run_async_job() は敢えて
    # オーバーライドしない。base.run_async_job() は status=="done" を完了とみなす
    # 別の契約（pending/running/done/failed）を前提にした汎用ポーリングループだが、
    # このバックエンドはCLIの `generate wait` が完了までブロックする方式のため
    # status=="completed" を使う独自フロー(generate()内で直接wait_for_resultを呼ぶ)
    # を使う。もし poll_job を "互換" のつもりでエイリアスすると、base.run_async_job
    # 経由で呼ばれた場合に "completed" が "done" と一致せず完了と判定されないまま
    # タイムアウトするバグになるため、意図的に実装しない
    # （呼べば基底クラスの NotImplementedError で明確に失敗する）。

    # -- 統合フロー --------------------------------------------------------

    def generate(self, shot: dict, out_path: str) -> dict:
        cost = self.estimate_cost(shot)
        if cost > self.max_credits_per_shot:
            raise HiggsfieldCostLimitError(
                "higgsfield: 見積コスト{}クレジットがmax_credits_per_shot({})を超えるため、"
                "ジョブを投入せず中断しました(shot_id={})。config.jsonのhiggsfield.max_credits_per_shot"
                "を見直すか、ショットのduration/解像度を下げてください。".format(
                    cost, self.max_credits_per_shot, shot.get("id")
                )
            )

        job_id = self.submit_job(shot)
        result = self.wait_for_result(job_id)
        status = result["status"]

        if status == "failed":
            raise HiggsfieldJobFailedError(
                "higgsfield ジョブ失敗 (job_id={}): {}".format(job_id, result.get("error") or "不明なエラー")
            )
        if status != "completed":
            raise HiggsfieldTimeoutError(
                "higgsfield ジョブが完了ステータスになりませんでした (job_id={}, status={})".format(
                    job_id, status
                )
            )

        self.fetch_result(job_id, out_path, result_url=result.get("result_url"))
        return {
            "backend": self.name,
            "shot_id": shot.get("id"),
            "job_id": job_id,
            "status": status,
            "credits_estimated": cost,
            "result_url": result.get("result_url"),
        }
