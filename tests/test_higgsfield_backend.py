# -*- coding: utf-8 -*-
"""higgsfield_backend の単体テスト（subprocessをモック・実CLI呼び出しなし）。"""
import json
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


# --- image_path / reference_images（image-to-video） ---------------------------

def test_build_create_cmd_adds_start_image_when_image_path_present():
    cmd = hb._build_create_cmd(
        "higgsfield", "seedance_2_0_mini", _shot(image_path="/tmp/product.png"), "480p"
    )
    idx = cmd.index("--start-image")
    assert cmd[idx + 1] == "/tmp/product.png"


def test_build_cost_cmd_adds_start_image_when_image_path_present():
    cmd = hb._build_cost_cmd(
        "higgsfield", "seedance_2_0_mini", _shot(image_path="/tmp/product.png"), "480p"
    )
    idx = cmd.index("--start-image")
    assert cmd[idx + 1] == "/tmp/product.png"


def test_build_create_cmd_without_image_path_is_unchanged_regression():
    """画像なしshotのコマンドは従来と完全に同一(--start-image/--image-referencesを含まない)。"""
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", _shot(), "480p")
    assert cmd == [
        "higgsfield", "generate", "create", "seedance_2_0_mini",
        "--prompt", _shot()["visual_prompt"],
        "--aspect-ratio", "9:16",
        "--resolution", "480p",
        "--duration", "5",
        "--json",
    ]
    assert "--start-image" not in cmd
    assert "--image-references" not in cmd


def test_build_cost_cmd_without_image_path_is_unchanged_regression():
    cmd = hb._build_cost_cmd("higgsfield", "seedance_2_0_mini", _shot(), "480p")
    assert cmd == [
        "higgsfield", "generate", "cost", "seedance_2_0_mini",
        "--prompt", _shot()["visual_prompt"],
        "--aspect-ratio", "9:16",
        "--resolution", "480p",
        "--duration", "5",
        "--json",
    ]
    assert "--start-image" not in cmd
    assert "--image-references" not in cmd


def test_build_create_cmd_repeats_image_references_flag_per_image():
    refs = ["/tmp/ref1.png", "/tmp/ref2.png", "/tmp/ref3.png"]
    cmd = hb._build_create_cmd(
        "higgsfield", "seedance_2_0_mini", _shot(reference_images=refs), "480p"
    )
    assert cmd.count("--image-references") == 3
    positions = [i for i, v in enumerate(cmd) if v == "--image-references"]
    values = [cmd[i + 1] for i in positions]
    assert values == refs


def test_build_create_cmd_clips_reference_images_to_max_9():
    refs = ["/tmp/ref{}.png".format(i) for i in range(15)]
    cmd = hb._build_create_cmd(
        "higgsfield", "seedance_2_0_mini", _shot(reference_images=refs), "480p"
    )
    assert cmd.count("--image-references") == 9
    positions = [i for i, v in enumerate(cmd) if v == "--image-references"]
    values = [cmd[i + 1] for i in positions]
    assert values == refs[:9]


def test_build_create_cmd_combines_start_image_and_reference_images():
    cmd = hb._build_create_cmd(
        "higgsfield", "seedance_2_0_mini",
        _shot(image_path="/tmp/product.png", reference_images=["/tmp/ref1.png"]),
        "480p",
    )
    assert cmd.index("--start-image") < cmd.index("--json")
    assert cmd.count("--image-references") == 1


# --- image_pathの実在チェック(生成前・日本語エラー) ---------------------------

def test_generate_raises_japanese_error_when_image_path_missing(tmp_path):
    backend = hb.HiggsfieldBackend(_cfg())
    missing_path = str(tmp_path / "not_exists.png")
    with pytest.raises(VisualBackendError) as excinfo:
        backend.generate(_shot(image_path=missing_path), "/tmp/out.mp4")
    message = str(excinfo.value)
    assert "商品画像が見つかりません" in message
    assert missing_path in message


def test_generate_does_not_call_cli_when_image_path_missing(tmp_path, monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    missing_path = str(tmp_path / "not_exists.png")

    def _fail(*args, **kwargs):
        raise AssertionError("image_pathが存在しないのにCLIを呼んではいけない")

    monkeypatch.setattr(backend, "estimate_cost", _fail)
    with pytest.raises(VisualBackendError):
        backend.generate(_shot(image_path=missing_path), "/tmp/out.mp4")


# --- 安全リトライ中もimage_pathを維持する --------------------------------------

def test_generate_keeps_image_path_across_safety_retry(monkeypatch, tmp_path):
    """1回目nsfw拒否→2回目(安全プレフィックス版)でも --start-image が維持されることを検証する。"""
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    image_path = str(tmp_path / "product.png")
    with open(image_path, "wb") as f:
        f.write(b"fake-png-bytes")

    calls = []
    create_cmds = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            create_cmds.append(cmd)
            job_id = "job-attempt-{}".format(len(create_cmds))
            return _FakeProc(returncode=0, stdout=json.dumps([job_id]).encode("utf-8"))
        if cmd[2] == "wait":
            job_id = cmd[3]
            if job_id == "job-attempt-1":
                return _FakeProc(returncode=1, stderr=b'Error: job job-attempt-1 ended with status "nsfw"')
            return _FakeProc(
                returncode=0,
                stdout=b'{"status": "completed", "result_url": "https://example.com/a.mp4"}',
            )
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)
    monkeypatch.setattr(hb, "_download_url", lambda url, path, curl_bin=None, timeout_sec=120: None)

    shot = _shot(
        image_path=image_path,
        visual_prompt="bold warning-style graphic with a soft yellow gradient background and a simple exclamation icon",
    )
    meta = backend.generate(shot, out_path)

    assert len(create_cmds) == 2
    for cmd in create_cmds:
        idx = cmd.index("--start-image")
        assert cmd[idx + 1] == image_path  # 安全リトライ2回目でもimage_pathが維持されている
    assert meta["status"] == "completed"
    assert meta["safety_retry_attempt"] == 2


# --- BUG-10: モデル最小/最大duration制約に合わせたクランプ ---------------------

def test_resolve_request_duration_sec_clamps_below_minimum_to_4_bug10():
    # 実機で `duration: Input should be greater than or equal to 4` が発生した3秒ショット。
    assert hb._resolve_request_duration_sec(_shot(duration_sec=3)) == 4


def test_resolve_request_duration_sec_clamps_above_maximum_to_12_bug10():
    assert hb._resolve_request_duration_sec(_shot(duration_sec=18)) == 12


def test_resolve_request_duration_sec_ceils_fractional_seconds_bug10():
    # ceil(4.1)=5。round(4.1)=4 になる旧実装との違いを明示する回帰テスト。
    assert hb._resolve_request_duration_sec(_shot(duration_sec=4.1)) == 5


def test_resolve_request_duration_sec_keeps_value_within_range_unchanged_bug10():
    assert hb._resolve_request_duration_sec(_shot(duration_sec=7)) == 7


def test_build_create_cmd_clamps_duration_below_minimum_to_4_bug10():
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", _shot(duration_sec=3), "480p")
    idx = cmd.index("--duration")
    assert cmd[idx + 1] == "4"


def test_build_create_cmd_clamps_duration_above_maximum_to_12_bug10():
    cmd = hb._build_create_cmd("higgsfield", "seedance_2_0_mini", _shot(duration_sec=18), "480p")
    idx = cmd.index("--duration")
    assert cmd[idx + 1] == "12"


def test_build_cost_cmd_clamps_duration_below_minimum_to_4_bug10():
    # コスト見積とジョブ投入は同じduration値を使うべき（実際に課金される値と見積が一致する）。
    cmd = hb._build_cost_cmd("higgsfield", "seedance_2_0_mini", _shot(duration_sec=3), "480p")
    idx = cmd.index("--duration")
    assert cmd[idx + 1] == "4"


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


def test_run_cli_raises_japanese_credit_error_when_stderr_says_not_enough_credits(monkeypatch):
    def fake_run(cmd, stdout, stderr, shell, timeout):
        return _FakeProc(returncode=1, stderr=b'Error: {"error": "not_enough_credits", "credits_required": 10}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(VisualBackendError) as excinfo:
        hb._run_cli(["higgsfield", "generate", "create", "m", "--json"], timeout_sec=10)
    message = str(excinfo.value)
    assert "クレジットが不足しています" in message
    assert "https://higgsfield.ai/" in message
    assert "続きから作成する" in message
    assert isinstance(excinfo.value, hb.HiggsfieldCreditError)  # 専用例外クラスで送出される
    assert not isinstance(excinfo.value, hb.HiggsfieldAuthError)
    assert not isinstance(excinfo.value, hb.HiggsfieldTimeoutError)


def test_submit_job_does_not_retry_credit_error_even_when_stderr_also_contains_connection_text(monkeypatch):
    """独立レビュー対応: stderrに"connection"（transientパターン）と"not_enough_credits"が
    同居していても、例外型(HiggsfieldCreditError)で明示的に判定するためリトライされない
    （日本語ラップ後のメッセージにstderr原文が埋め込まれ、そこに"connection"という文字列が
    混入していても_is_transient_errorのメッセージ一致に引きずられない）ことを検証する。
    """
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        return _FakeProc(
            returncode=1,
            stderr=b'Error: connection unstable: {"error": "not_enough_credits"}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        hb.time, "sleep",
        lambda sec: (_ for _ in ()).throw(
            AssertionError("connectionが混入したクレジット不足エラーはsleep(リトライ)してはいけない")
        ),
    )

    with pytest.raises(hb.HiggsfieldCreditError):
        backend.submit_job(_shot())

    assert len(calls) == 1  # 再試行していない


def test_submit_job_does_not_retry_credit_error(monkeypatch):
    """クレジット不足はユーザーのチャージが必要で自動復旧しないため、安全リトライ(nsfw)や
    transientリトライの対象にせず即座に失敗させる（sleepが呼ばれない=リトライしていない）。"""
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        return _FakeProc(returncode=1, stderr=b'Error: {"error": "not_enough_credits"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        hb.time, "sleep",
        lambda sec: (_ for _ in ()).throw(AssertionError("クレジット不足エラーはsleep(リトライ)してはいけない")),
    )

    with pytest.raises(hb.HiggsfieldCreditError) as excinfo:
        backend.submit_job(_shot())

    assert len(calls) == 1  # 再試行していない
    assert "クレジットが不足しています" in str(excinfo.value)


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


# --- 一時的エラーの判定 -------------------------------------------------------

def test_is_transient_error_matches_no_response_received():
    assert hb._is_transient_error("Error: request failed (no response received)") is True


def test_is_transient_error_matches_connection_case_insensitive():
    assert hb._is_transient_error("Connection RESET by peer") is True


def test_is_transient_error_false_for_validation_error():
    assert hb._is_transient_error("duration: Input should be greater than or equal to 4") is False


def test_is_transient_error_false_for_unrelated_error():
    assert hb._is_transient_error("model not found") is False


# --- リトライ: (a) createが一時エラー→リトライ→成功 -----------------------------

def test_submit_job_retries_on_transient_error_then_succeeds(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []
    sleeps = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeProc(returncode=3, stderr=b"Error: request failed (no response received)")
        return _FakeProc(returncode=0, stdout=b'["job-success-1"]')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: sleeps.append(sec))

    job_id = backend.submit_job(_shot())

    assert job_id == "job-success-1"
    assert len(calls) == 2
    assert all(c[2] == "create" for c in calls)  # createコマンドが2回とも呼ばれた(create自体の再試行)
    assert sleeps == [5]  # 1回目のリトライは5秒バックオフ


def test_submit_job_gives_up_after_max_retries_and_reports_attempt_count(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        return _FakeProc(returncode=3, stderr=b"Error: request failed (no response received)")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)

    with pytest.raises(VisualBackendError) as excinfo:
        backend.submit_job(_shot())

    assert len(calls) == 3  # 初回 + 最大2回リトライ = 計3回
    assert "3" in str(excinfo.value)  # 試行回数がメッセージに含まれる


# --- リトライ: (b) 認証エラーは非リトライ -------------------------------------

def test_submit_job_does_not_retry_auth_error(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        return _FakeProc(returncode=1, stderr=b"Error: Not authenticated. Run `higgsfield auth login`.")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: (_ for _ in ()).throw(AssertionError("認証エラーはsleepしてはいけない")))

    with pytest.raises(hb.HiggsfieldAuthError):
        backend.submit_job(_shot())

    assert len(calls) == 1  # 再試行していない


def test_submit_job_does_not_retry_validation_error(monkeypatch):
    backend = hb.HiggsfieldBackend(_cfg())
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        return _FakeProc(returncode=3, stderr=b"duration: Input should be greater than or equal to 4")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: (_ for _ in ()).throw(AssertionError("バリデーションエラーはsleepしてはいけない")))

    with pytest.raises(VisualBackendError):
        backend.submit_job(_shot())

    assert len(calls) == 1  # 再試行していない


# --- リトライ: (c) wait失敗はwaitのみ再試行(createは1回のみ=課金1回) -------------

def test_generate_retries_wait_only_without_resubmitting_create(monkeypatch, tmp_path):
    """createは1回しか呼ばれない(=二重課金しない)ことを、実際のCLI呼び出しレベルで検証する。"""
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            return _FakeProc(returncode=0, stdout=b'["job-once"]')
        if cmd[2] == "wait":
            wait_calls = [c for c in calls if c[2] == "wait"]
            if len(wait_calls) == 1:
                return _FakeProc(returncode=3, stderr=b"Error: request failed (no response received)")
            return _FakeProc(
                returncode=0,
                stdout=b'{"status": "completed", "result_url": "https://example.com/a.mp4"}',
            )
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)
    monkeypatch.setattr(hb, "_download_url", lambda url, path, curl_bin=None, timeout_sec=120: None)

    meta = backend.generate(_shot(), out_path)

    create_calls = [c for c in calls if c[2] == "create"]
    wait_calls = [c for c in calls if c[2] == "wait"]
    assert len(create_calls) == 1  # createは1回のみ=課金1回
    assert len(wait_calls) == 2  # waitは1回失敗→1回リトライで成功
    assert meta["job_id"] == "job-once"
    assert meta["status"] == "completed"


# --- 安全フィルタ(nsfw)誤検知への段階的リトライ(2026-07-17実機確認) --------------

# (a) nsfw検知判定(文字列バリエーション) --------------------------------------

def test_is_safety_rejection_detects_nsfw_status_message():
    assert hb._is_safety_rejection('Error: job 2296cac2-abc ended with status "nsfw"') is True


def test_is_safety_rejection_detects_moderation_case_insensitive():
    assert hb._is_safety_rejection("Content flagged by MODERATION system") is True


def test_is_safety_rejection_detects_flagged_marker():
    assert hb._is_safety_rejection("this request was flagged") is True


def test_is_safety_rejection_false_for_unrelated_error():
    assert hb._is_safety_rejection("model not found") is False


def test_is_safety_rejection_false_for_ordinary_job_failure():
    assert hb._is_safety_rejection("content policy violation") is False


# --- プロンプト言い換えの組み立て --------------------------------------------

def test_build_safety_retry_prompt_first_attempt_keeps_original():
    assert hb._build_safety_retry_prompt("calm ocean sunrise", 1) == "calm ocean sunrise"


def test_build_safety_retry_prompt_second_attempt_adds_safe_prefix():
    result = hb._build_safety_retry_prompt("calm ocean sunrise", 2)
    assert result == "wholesome, family-friendly, safe-for-work, calm ocean sunrise"


def test_build_safety_retry_prompt_third_attempt_uses_abstract_fallback():
    result = hb._build_safety_retry_prompt("calm ocean sunrise", 3)
    assert "abstract geometric shapes" in result
    assert "vertical 9:16" in result
    assert "calm ocean sunrise" not in result


# (b) 1回目nsfw→安全プレフィックス付きで2回目成功(createに渡るプロンプトを実際にアサート) --

def test_generate_retries_with_safety_prompt_after_nsfw_rejection_then_succeeds(monkeypatch, tmp_path):
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    calls = []
    create_prompts = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            idx = cmd.index("--prompt")
            create_prompts.append(cmd[idx + 1])
            job_id = "job-attempt-{}".format(len(create_prompts))
            return _FakeProc(returncode=0, stdout=json.dumps([job_id]).encode("utf-8"))
        if cmd[2] == "wait":
            job_id = cmd[3]
            if job_id == "job-attempt-1":
                return _FakeProc(returncode=1, stderr=b'Error: job job-attempt-1 ended with status "nsfw"')
            return _FakeProc(
                returncode=0,
                stdout=b'{"status": "completed", "result_url": "https://example.com/a.mp4"}',
            )
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)
    monkeypatch.setattr(hb, "_download_url", lambda url, path, curl_bin=None, timeout_sec=120: None)

    shot = _shot(
        visual_prompt="bold warning-style graphic with a soft yellow gradient background and a simple exclamation icon"
    )
    meta = backend.generate(shot, out_path)

    assert len(create_prompts) == 2  # 安全リトライで2回目のcreateが発火した
    assert create_prompts[0] == shot["visual_prompt"]
    assert create_prompts[1] == "wholesome, family-friendly, safe-for-work, " + shot["visual_prompt"]
    assert meta["status"] == "completed"
    assert meta["job_id"] == "job-attempt-2"
    assert meta["safety_retry_attempt"] == 2


# (c) 3回全滅→日本語エラー ----------------------------------------------------

def test_generate_raises_japanese_error_after_three_safety_rejections(monkeypatch, tmp_path):
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            return _FakeProc(returncode=0, stdout=b'["job-x"]')
        if cmd[2] == "wait":
            return _FakeProc(returncode=1, stderr=b'Error: job job-x ended with status "nsfw"')
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)

    shot = _shot(
        visual_prompt=(
            "bold warning-style graphic with a soft yellow gradient background and a simple "
            "exclamation icon, clean modern design, vertical 9:16, extra padding text for length"
        )
    )
    with pytest.raises(VisualBackendError) as excinfo:
        backend.generate(shot, out_path)

    message = str(excinfo.value)
    assert "安全フィルタ" in message
    assert shot["visual_prompt"][:80] in message

    create_calls = [c for c in calls if c[2] == "create"]
    assert len(create_calls) == 3  # 初回 + 安全リトライ2回 = 計3回


# (d) 通常エラーは安全リトライしない -------------------------------------------

def test_generate_does_not_retry_with_safety_prompt_for_ordinary_failure(monkeypatch, tmp_path):
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    calls = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            return _FakeProc(returncode=0, stdout=b'["job-y"]')
        if cmd[2] == "wait":
            return _FakeProc(
                returncode=0,
                stdout=b'{"status": "failed", "error": "content policy violation"}',
            )
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        hb.time, "sleep",
        lambda sec: (_ for _ in ()).throw(AssertionError("通常失敗で安全リトライのsleepは不要")),
    )

    with pytest.raises(hb.HiggsfieldJobFailedError):
        backend.generate(_shot(), out_path)

    create_calls = [c for c in calls if c[2] == "create"]
    assert len(create_calls) == 1  # 安全リトライは発火しない=createは1回のみ


# (e) transientリトライとの非干渉 ----------------------------------------------

def test_transient_retry_still_works_inside_a_safety_retry_attempt(monkeypatch, tmp_path):
    """1回目nsfw(安全リトライ発火)→2回目(安全プレフィックス版)のwaitが一時的エラーで
    1回失敗してから、既存のtransientリトライ機構で同一プロンプトのまま自動復旧することを検証する。
    安全リトライ(プロンプト差し替え)とtransientリトライ(同一プロンプト再試行)が別レイヤーとして
    正しく共存することの確認。
    """
    backend = hb.HiggsfieldBackend(_cfg())
    out_path = str(tmp_path / "out.mp4")
    calls = []
    create_prompts = []

    def fake_run(cmd, stdout, stderr, shell, timeout):
        calls.append(cmd)
        if cmd[2] == "cost":
            return _FakeProc(returncode=0, stdout=b'{"credits": 5}')
        if cmd[2] == "create":
            idx = cmd.index("--prompt")
            create_prompts.append(cmd[idx + 1])
            job_id = "job-attempt-{}".format(len(create_prompts))
            return _FakeProc(returncode=0, stdout=json.dumps([job_id]).encode("utf-8"))
        if cmd[2] == "wait":
            job_id = cmd[3]
            wait_calls_for_job = [c for c in calls if c[2] == "wait" and c[3] == job_id]
            if job_id == "job-attempt-1":
                return _FakeProc(returncode=1, stderr=b'Error: job job-attempt-1 ended with status "nsfw"')
            if len(wait_calls_for_job) == 1:
                return _FakeProc(returncode=3, stderr=b"Error: request failed (no response received)")
            return _FakeProc(
                returncode=0,
                stdout=b'{"status": "completed", "result_url": "https://example.com/a.mp4"}',
            )
        raise AssertionError("想定外のコマンド: {}".format(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(hb.time, "sleep", lambda sec: None)
    monkeypatch.setattr(hb, "_download_url", lambda url, path, curl_bin=None, timeout_sec=120: None)

    meta = backend.generate(_shot(), out_path)

    assert len(create_prompts) == 2  # 安全リトライは1回だけ発火(createは計2回)
    wait_job2_calls = [c for c in calls if c[2] == "wait" and c[3] == "job-attempt-2"]
    assert len(wait_job2_calls) == 2  # job-attempt-2に対してtransientリトライが1回働いた
    assert meta["status"] == "completed"
    assert meta["job_id"] == "job-attempt-2"
