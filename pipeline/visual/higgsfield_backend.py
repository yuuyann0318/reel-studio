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
import time

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


_MAX_REFERENCE_IMAGES = 9


def _build_image_args(shot):
    """shotの image_path / reference_images から画像関連の追加CLIフラグを構築する（副作用なし・テスト対象）。

    - image_path（商品画像のローカル絶対パス、非空）: `--start-image <path>` を1つ追加
      （image-to-video。Higgsfield CLIはローカルパスを自動アップロードする＝実機確認済み）。
    - reference_images（list、任意）: 最大 `_MAX_REFERENCE_IMAGES`(9) 枚にクリップし、
      1枚ごとに `--image-references <path>` を繰り返し追加する（CLIヘルプ実測:
      "use repeated --image-references"）。
    - どちらも無い/空のshotでは何も追加しない＝画像なしshotのコマンドは従来と完全に同一（回帰）。
    """
    args = []
    image_path = shot.get("image_path")
    if image_path:
        args += ["--start-image", str(image_path)]
    reference_images = shot.get("reference_images") or []
    for ref_path in list(reference_images)[:_MAX_REFERENCE_IMAGES]:
        args += ["--image-references", str(ref_path)]
    return args


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


class HiggsfieldSafetyRejectedError(VisualBackendError):
    """安全フィルタ(nsfw/moderation/flagged)によりジョブが拒否された場合（1回の試行内部で使う中間例外）。

    generate() 内の安全リトライループがこれを捕捉してプロンプトを差し替え再試行する。
    最終試行でも拒否された場合は generate() が日本語の VisualBackendError に変換して送出する。
    """


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
    ] + _build_image_args(shot) + ["--json"]


def _build_cost_cmd(cli_bin, model, shot, resolution):
    """コスト見積コマンドを構築する（副作用なし・テスト対象）。

    課金額の見積とジョブ投入(_build_create_cmd)は、画像フラグを含め同じ入力条件で
    行うべき(見積と実際の課金対象が一致する)ため、_build_image_args を共通利用する。
    """
    duration_sec = _resolve_request_duration_sec(shot)
    return [
        cli_bin, "generate", "cost", model,
        "--prompt", shot.get("visual_prompt", ""),
        "--aspect-ratio", "9:16",
        "--resolution", resolution,
        "--duration", str(duration_sec),
    ] + _build_image_args(shot) + ["--json"]


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


# ★一時的接続失敗への耐性（実機確認・2026-07-17）: `higgsfield generate create` が
# `Error: request failed (no response received)` という一時的な接続失敗(exit 3)で
# 落ちることがあり、直後にCLIを叩くと正常に通る。14ショット中1回でも起きると全体が
# 失敗していたため、指数バックオフ付きリトライで吸収する。認証エラー
# (`_is_auth_error`)やバリデーションエラー(例: "Input should be")はここに該当せず、
# 再試行しても無駄なので即座に失敗させる。
_TRANSIENT_ERROR_PATTERNS = (
    "no response received",
    "request failed",
    "timed out",
    "timeout",
    "connection",
)

# リトライ回数・バックオフ秒数(5秒→15秒)。要素数=最大リトライ回数。
_DEFAULT_RETRY_BACKOFF_SEC = (5, 15)


def _is_transient_error(text):
    """例外メッセージ(str化したもの)が一時的エラーのパターンに一致するか判定する。"""
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in _TRANSIENT_ERROR_PATTERNS)


# ★安全フィルタ誤検知への耐性（実機確認・2026-07-17）: 完全に無害なプロンプト
# （例: "bold warning-style graphic with a soft yellow gradient background and a
# simple exclamation icon, clean modern design, vertical 9:16"）で
# `Error: job <uuid> ended with status "nsfw"` が返ることがある(安全フィルタの誤検知)。
# これは_TRANSIENT_ERROR_PATTERNSには一致しない(同一プロンプトの単純再試行は無意味な
# ため、意図的にtransient扱いにしない)。代わりにプロンプトを段階的に安全側へ言い換えて
# create からやり直す専用リトライを generate() 内に持つ。
_SAFETY_REJECTION_MARKERS = ("nsfw", "moderation", "flagged")

# 安全リトライで使う言い換えプロンプト。2回目は元プロンプトに安全側の接頭辞を付加、
# 3回目(最終)は常に安全な抽象的ビジュアルへ完全差し替えする(動画として破綻しない汎用画)。
_SAFETY_RETRY_PREFIX = "wholesome, family-friendly, safe-for-work, "
_SAFETY_RETRY_FALLBACK_PROMPT = (
    "abstract geometric shapes, soft gradient background, clean minimal modern design, vertical 9:16"
)
_MAX_SAFETY_ATTEMPTS = 3


def _is_safety_rejection(text):
    """wait結果のstatus/errorやCLIの非ゼロ終了エラーメッセージが、安全フィルタ(nsfw/moderation/
    flagged)による拒否を示しているか判定する（副作用なし・テスト対象）。

    例: `Error: job <uuid> ended with status "nsfw"` や、statusフィールドそのものが
    "nsfw"/"moderation"/"flagged" のケースの両方をこの1関数でカバーする。
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SAFETY_REJECTION_MARKERS)


def _build_safety_retry_prompt(original_prompt, attempt):
    """安全フィルタ拒否後の再試行プロンプトを組み立てる（副作用なし・テスト対象）。

    attempt: 1始まりの試行回数。
      1回目: 元プロンプトのまま。
      2回目: 元プロンプトの先頭に安全側の接頭辞を付加。
      3回目(最終): 常に安全な抽象プロンプトへ完全差し替え。
    """
    if attempt <= 1:
        return original_prompt
    if attempt == 2:
        return _SAFETY_RETRY_PREFIX + original_prompt
    return _SAFETY_RETRY_FALLBACK_PROMPT


def _call_with_retry(fn, label, max_retries=2, backoff_sec=_DEFAULT_RETRY_BACKOFF_SEC, sleep_fn=None):
    """fn()を実行し、一時的エラーの場合のみ指数バックオフで最大max_retries回まで再試行する。

    - `HiggsfieldAuthError`（認証切れ）は一時的エラーではないため即座に再送出する（再試行しない）。
    - それ以外の `VisualBackendError` は、メッセージが `_is_transient_error` に一致する場合のみ
      リトライ対象。一致しない場合（バリデーションエラー等）は即座に再送出する。
    - `sleep_fn` はテストでmonkeypatchできるよう外部注入可能にしてある（未指定時は `time.sleep`
      をその都度参照するため、`time.sleep` 自体をmonkeypatchしても効く）。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except HiggsfieldAuthError:
            raise
        except VisualBackendError as exc:
            if not _is_transient_error(str(exc)):
                raise
            if attempt >= max_retries:
                raise VisualBackendError(
                    "{}が一時的エラーで失敗しました(試行{}回・最大{}回のリトライを含む): {}".format(
                        label, attempt + 1, max_retries, str(exc)[:500]
                    )
                )
            sleep = sleep_fn or time.sleep
            sleep(backoff_sec[min(attempt, len(backoff_sec) - 1)])
            attempt += 1


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
        # ★job_id取得前（=まだジョブが投入できていない/課金が確定していない段階）の
        # 一時的エラーのみをここでリトライする。二重課金防止のため、job_id取得後は
        # このメソッドを呼び直さない（wait側の再試行は wait_for_result 内で完結させる）。
        def _do():
            cmd = _build_create_cmd(self.cli_bin, self.model, shot, self.resolution)
            stdout = _run_cli(cmd, timeout_sec=60)
            return _parse_job_id(stdout)

        return _call_with_retry(_do, label="higgsfield generate create")

    def wait_for_result(self, job_id: str) -> dict:
        # ★waitのみ再試行（二重課金防止）: createは既に成功しjob_idを取得済みのため、
        # ここで一時的エラーが起きてもcreateをやり直さず、同じjob_idに対するwaitだけを
        # 再試行する。
        def _do():
            cmd = _build_wait_cmd(self.cli_bin, job_id, self.poll_timeout_sec, self.poll_interval_sec)
            # Python側subprocessタイムアウトはCLI自身の--timeoutより余裕を持たせる（CLI側の
            # タイムアウト処理を正としつつ、CLIが応答不能になった場合の保険として使う）。
            stdout = _run_cli(cmd, timeout_sec=self.poll_timeout_sec + 60)
            return _parse_wait_result(stdout)

        return _call_with_retry(_do, label="higgsfield generate wait(job_id={})".format(job_id))

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

    def _generate_once(self, shot: dict, out_path: str) -> dict:
        """1回分の生成試行(cost見積→submit→wait→fetch)。

        安全フィルタ拒否(nsfw/moderation/flagged)を検知した場合は
        `HiggsfieldSafetyRejectedError` を送出する(generate()の安全リトライループが捕捉する)。
        それ以外の失敗(認証切れ/コスト超過/通常のジョブ失敗/タイムアウト)は従来どおり
        該当の例外型でそのまま送出する。
        """
        cost = self.estimate_cost(shot)
        if cost > self.max_credits_per_shot:
            raise HiggsfieldCostLimitError(
                "higgsfield: 見積コスト{}クレジットがmax_credits_per_shot({})を超えるため、"
                "ジョブを投入せず中断しました(shot_id={})。config.jsonのhiggsfield.max_credits_per_shot"
                "を見直すか、ショットのduration/解像度を下げてください。".format(
                    cost, self.max_credits_per_shot, shot.get("id")
                )
            )

        try:
            job_id = self.submit_job(shot)
            result = self.wait_for_result(job_id)
        except HiggsfieldAuthError:
            raise
        except VisualBackendError as exc:
            if _is_safety_rejection(str(exc)):
                raise HiggsfieldSafetyRejectedError(str(exc))
            raise

        status = result["status"]

        if status == "failed":
            error_text = result.get("error") or "不明なエラー"
            if _is_safety_rejection(error_text):
                raise HiggsfieldSafetyRejectedError(error_text)
            raise HiggsfieldJobFailedError(
                "higgsfield ジョブ失敗 (job_id={}): {}".format(job_id, error_text)
            )
        if _is_safety_rejection(status):
            raise HiggsfieldSafetyRejectedError("status={}".format(status))
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

    def generate(self, shot: dict, out_path: str) -> dict:
        # ★image_pathの実在チェックは全リトライ試行に共通の前提条件のため、安全リトライ
        # ループに入る前に1回だけ行う(image_pathは全試行を通じて不変=attempt_shotでも
        # dict(shot)によりそのまま維持される。差し替わるのはvisual_promptのみ)。
        image_path = shot.get("image_path")
        if image_path and not os.path.isfile(image_path):
            raise VisualBackendError("商品画像が見つかりません: {}".format(image_path))

        # ★安全フィルタ誤検知対策(2026-07-17実機確認): nsfw等で拒否されたら、プロンプトを
        # 段階的に安全側へ言い換えてcreateからやり直す。nsfw失敗はクレジット未消課金のため
        # (実測で残高確認済み)、createの再試行は二重課金にならない。
        # ★image_path/reference_imagesは安全リトライでも維持する(言い換えるのはpromptのみ。
        # 3回目の抽象プロンプト差し替え時も商品画像は落とさない=商品画像が主役のため)。
        original_prompt = shot.get("visual_prompt", "")
        last_safety_error = None
        for attempt in range(1, _MAX_SAFETY_ATTEMPTS + 1):
            attempt_shot = dict(shot)
            attempt_shot["visual_prompt"] = _build_safety_retry_prompt(original_prompt, attempt)
            try:
                meta = self._generate_once(attempt_shot, out_path)
            except HiggsfieldSafetyRejectedError as exc:
                last_safety_error = exc
                if attempt < _MAX_SAFETY_ATTEMPTS:
                    continue
                raise VisualBackendError(
                    "映像AIの安全フィルタで拒否されました(誤検知の可能性)。シーンの説明"
                    "(visual_prompt)を変えて再試行してください: {}"
                    "(安全リトライ{}回すべて拒否・最終試行のエラー: {})".format(
                        original_prompt[:80], _MAX_SAFETY_ATTEMPTS, str(last_safety_error)[:300]
                    )
                )
            meta["safety_retry_attempt"] = attempt
            meta["prompt_used"] = attempt_shot["visual_prompt"]
            return meta
        # 理論上到達しない(ループは必ずreturnかraiseで抜ける)。
        raise VisualBackendError("higgsfield: 安全リトライ処理で予期しない状態になりました。")
