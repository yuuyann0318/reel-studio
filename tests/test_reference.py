# -*- coding: utf-8 -*-
"""pipeline/reference.py のテスト。実ネットワーク/実yt-dlp/実ASR/実claude呼び出しは一切行わない
（すべてDIでモック注入するか、subprocess.Popen/urlopen自体をmonkeypatchする）。
"""
import json
import os
import subprocess
import urllib.error

import pytest

from pipeline import reference as ref


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------

def test_normalize_url_strips_query_and_fragment():
    url = "https://www.tiktok.com/@user/video/123?foo=bar&baz=qux#frag"
    assert ref.normalize_url(url) == "https://www.tiktok.com/@user/video/123"


def test_normalize_url_idempotent_without_query():
    url = "https://www.tiktok.com/@user/video/123"
    assert ref.normalize_url(url) == url


def test_normalize_url_handles_empty_and_none():
    assert ref.normalize_url("") == ""
    assert ref.normalize_url(None) == ""


def test_normalize_url_query_only_variants_collapse_to_same_key():
    a = ref.normalize_url("https://x.com/v/1?a=1")
    b = ref.normalize_url("https://x.com/v/1?a=2#frag")
    assert a == b == "https://x.com/v/1"


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------

def _cfg_with_cache_dir(tmp_path):
    return {"reference": {"cache_dir": str(tmp_path / "cache")}}


def test_cache_hit_skips_all_external_calls(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    url = "https://x.com/a"
    cache_path = ref._cache_path_for(ref.normalize_url(url), cfg)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    cached_spec = {"version": 1, "url": url, "transcript": "cached"}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cached_spec, f)

    def _boom(*args, **kwargs):
        raise AssertionError("キャッシュヒット時は外部呼び出しが発生してはいけない")

    result = ref.analyze_reference(url, cfg=cfg, fetch_audio=_boom, asr_post=_boom, claude_call=_boom)
    assert result["ok"] is True
    assert result["cached"] is True
    assert result["source"] == "cache"
    assert result["spec"] == cached_spec


def test_cache_written_only_on_validation_success(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    url = "https://x.com/b"

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 20.0}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 20.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {
                "beats": [{"role": "hook", "start": 0, "end": 20, "text": "t", "summary": "s"}],
                "rhythm": {
                    "sentence_count": 3, "avg_sentence_len": 10, "max_sentence_len": 20,
                    "tone": "t", "endings": ["a"],
                },
            },
            "model_used": "m",
        }

    result = ref.analyze_reference(url, cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude)
    assert result["ok"] is True
    cache_path = ref._cache_path_for(ref.normalize_url(url), cfg)
    assert os.path.exists(cache_path)


def test_cache_dir_relative_override_resolves_against_project_root_not_cwd(monkeypatch, tmp_path):
    # 相対パスのoverrideは project_root() 基準で解決されるべき(CWD非依存)。
    # bin/ingest_reference.py の `project_root() / cache_dir` と同じ解決規律に統一する。
    other_cwd = tmp_path / "somewhere_else"
    other_cwd.mkdir()
    monkeypatch.chdir(str(other_cwd))

    cfg = {"reference": {"cache_dir": "assets/reference_cache"}}
    resolved = ref._cache_dir(cfg)

    assert resolved == str(ref.project_root() / "assets" / "reference_cache")
    assert not resolved.startswith(str(other_cwd))


def test_cache_dir_absolute_override_used_as_is(tmp_path):
    abs_override = str(tmp_path / "abs_cache")
    cfg = {"reference": {"cache_dir": abs_override}}
    assert ref._cache_dir(cfg) == abs_override


def test_cache_not_written_when_spec_validation_fails(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    url = "https://x.com/c"

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 20.0}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 20.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        # beatsが空 -> 検証NG
        return {"ok": True, "data": {"beats": [], "rhythm": {}}, "model_used": "m"}

    result = ref.analyze_reference(url, cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude)
    assert result["ok"] is False
    cache_path = ref._cache_path_for(ref.normalize_url(url), cfg)
    assert not os.path.exists(cache_path)


# ---------------------------------------------------------------------------
# default_fetch_audio: yt-dlpコマンド組立・タイムアウト・尺上限
# ---------------------------------------------------------------------------

class _FakePopen:
    """subprocess.Popenの代替。cmd/kwargsを記録し、communicate()の挙動を差し替え可能にする。"""

    captured_cmd = None
    captured_kwargs = None
    communicate_result = ("", "")
    raise_timeout = False
    write_dummy_output = False

    def __init__(self, cmd, **kwargs):
        _FakePopen.captured_cmd = cmd
        _FakePopen.captured_kwargs = kwargs
        self.returncode = 0
        self.pid = 99999
        self._cmd = cmd
        self.killed = False

    def communicate(self, timeout=None):
        _FakePopen.captured_timeout = timeout
        if _FakePopen.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=self._cmd, timeout=timeout)
        if _FakePopen.write_dummy_output:
            # -o の次の要素がoutput template
            idx = self._cmd.index("-o")
            template = self._cmd[idx + 1]
            out_path = template.replace("%(ext)s", "m4a")
            with open(out_path, "wb") as f:
                f.write(b"dummy-audio-bytes")
        return _FakePopen.communicate_result

    def terminate(self):
        self.killed = True


def _reset_fake_popen():
    _FakePopen.captured_cmd = None
    _FakePopen.captured_kwargs = None
    _FakePopen.communicate_result = ("", "")
    _FakePopen.raise_timeout = False
    _FakePopen.write_dummy_output = False


def test_default_fetch_audio_builds_expected_yt_dlp_command(monkeypatch):
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 30.0)

    cfg = {"reference": {"yt_dlp_bin": "/custom/yt-dlp", "download_timeout_sec": 45}}
    info = ref.default_fetch_audio("https://x.com/v/1", cfg)

    cmd = _FakePopen.captured_cmd
    assert cmd[0] == "/custom/yt-dlp"
    assert "-x" in cmd
    assert "--audio-format" in cmd
    assert "m4a" in cmd
    assert cmd[-1] == "https://x.com/v/1"
    assert _FakePopen.captured_timeout == 45
    assert info["duration_sec"] == 30.0
    assert os.path.exists(info["path"])


def test_default_fetch_audio_default_bin_and_timeout_when_not_configured(monkeypatch):
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 10.0)

    ref.default_fetch_audio("https://x.com/v/2", {})

    assert _FakePopen.captured_cmd[0] == "yt-dlp"
    assert _FakePopen.captured_timeout == ref.DEFAULT_DOWNLOAD_TIMEOUT_SEC


def test_default_fetch_audio_timeout_raises_and_kills_process(monkeypatch):
    _reset_fake_popen()
    _FakePopen.raise_timeout = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    killed = []
    monkeypatch.setattr(ref, "_kill_tree", lambda proc: killed.append(proc))

    with pytest.raises(RuntimeError) as exc_info:
        ref.default_fetch_audio("https://x.com/v/3", {"reference": {"download_timeout_sec": 5}})

    assert "タイムアウト" in str(exc_info.value)
    assert len(killed) == 1


def test_default_fetch_audio_nonzero_exit_raises(monkeypatch):
    _reset_fake_popen()

    class _FailingPopen(_FakePopen):
        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            self.returncode = 1

        def communicate(self, timeout=None):
            return ("", "ERROR: video unavailable")

    monkeypatch.setattr(ref.subprocess, "Popen", _FailingPopen)
    with pytest.raises(RuntimeError) as exc_info:
        ref.default_fetch_audio("https://x.com/v/4", {})
    assert "video unavailable" in str(exc_info.value)


def test_default_fetch_audio_rejects_over_max_video_sec(monkeypatch):
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 300.0)

    with pytest.raises(RuntimeError) as exc_info:
        ref.default_fetch_audio("https://x.com/v/5", {"reference": {"max_video_sec": 180}})
    assert "尺" in str(exc_info.value)


def test_default_fetch_audio_missing_output_file_raises(monkeypatch):
    _reset_fake_popen()
    # write_dummy_output=False のまま -> 出力ファイルが見つからない
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    with pytest.raises(RuntimeError) as exc_info:
        ref.default_fetch_audio("https://x.com/v/6", {})
    assert "見つかりません" in str(exc_info.value)


def test_default_fetch_audio_returns_tmp_dir_for_caller_cleanup(monkeypatch):
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 10.0)

    info = ref.default_fetch_audio("https://x.com/v/7", {})
    assert "tmp_dir" in info
    assert os.path.isdir(info["tmp_dir"])
    assert os.path.dirname(info["path"]) == info["tmp_dir"]


def test_default_fetch_audio_command_includes_separator_before_url(monkeypatch):
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 10.0)

    ref.default_fetch_audio("https://x.com/v/8", {})

    cmd = _FakePopen.captured_cmd
    assert cmd[-1] == "https://x.com/v/8"
    assert cmd[-2] == "--"


def test_default_fetch_audio_ffmpeg_location_option_precedes_separator(monkeypatch):
    # 回帰(codex-review指摘): --ffmpeg-locationは必ず"--"より前に置く必要がある。
    # "--"より後はyt-dlp側でオプション解釈されず、--ffmpeg-locationとその引数が
    # 追加のダウンロード対象URLとして誤解釈されてしまうバグがあった。
    _reset_fake_popen()
    _FakePopen.write_dummy_output = True
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ref, "_probe_duration", lambda ffprobe_bin, path: 10.0)

    cfg = {"ffmpeg_bin": "/some/dir/ffmpeg"}
    ref.default_fetch_audio("https://x.com/v/ffmpeg-loc", cfg)

    cmd = _FakePopen.captured_cmd
    assert "--ffmpeg-location" in cmd
    loc_idx = cmd.index("--ffmpeg-location")
    sep_idx = cmd.index("--")
    assert loc_idx < sep_idx, "「--ffmpeg-location」は「--」より前になければならない"
    assert cmd[loc_idx + 1] == "/some/dir"
    assert cmd[-1] == "https://x.com/v/ffmpeg-loc"
    assert cmd[-2] == "--"


def test_default_fetch_audio_rejects_non_http_scheme_url(monkeypatch):
    _reset_fake_popen()
    monkeypatch.setattr(ref.subprocess, "Popen", _FakePopen)

    with pytest.raises(RuntimeError) as exc_info:
        ref.default_fetch_audio("-o評価される様なURL", {})
    assert "スキーム" in str(exc_info.value)
    # スキーム検証はPopen呼び出し前に行われる(コマンド未実行のまま拒否される)
    assert _FakePopen.captured_cmd is None


def test_default_fetch_audio_cleans_tmp_dir_on_nonzero_exit(monkeypatch):
    _reset_fake_popen()

    class _FailingPopen(_FakePopen):
        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            self.returncode = 1

        def communicate(self, timeout=None):
            return ("", "ERROR: boom")

    monkeypatch.setattr(ref.subprocess, "Popen", _FailingPopen)
    created_dirs = []
    real_mkdtemp = ref.tempfile.mkdtemp

    def _tracking_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(ref.tempfile, "mkdtemp", _tracking_mkdtemp)

    with pytest.raises(RuntimeError):
        ref.default_fetch_audio("https://x.com/v/9", {})

    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])


# ---------------------------------------------------------------------------
# default_asr_post: Fish Audio ASR
# ---------------------------------------------------------------------------

def test_default_asr_post_success_parses_response(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    audio_path = tmp_path / "a.m4a"
    audio_path.write_bytes(b"dummy")

    def fake_post(url, path, api_key, timeout_sec=60):
        assert path == str(audio_path)
        assert api_key == "dummy-key"
        return json.dumps({"text": "こんにちは", "duration": 5.0, "segments": [{"text": "a", "start": 0, "end": 1}]}).encode("utf-8")

    result = ref.default_asr_post(str(audio_path), {}, _http_post=fake_post)
    assert result["ok"] is True
    assert result["text"] == "こんにちは"
    assert result["duration"] == 5.0
    assert result["segments"] == [{"text": "a", "start": 0, "end": 1}]


def test_default_asr_post_missing_api_key_returns_error_without_http_call(monkeypatch):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("APIキー未設定時はHTTP呼び出しが発生してはいけない")

    result = ref.default_asr_post("/tmp/a.m4a", {}, _http_post=_boom)
    assert result["ok"] is False
    assert "APIキー" in result["error"]


def test_default_asr_post_401_fails_immediately_without_retry(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    calls = {"n": 0}

    def fake_post(url, path, api_key, timeout_sec=60):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    result = ref.default_asr_post("/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: None)
    assert result["ok"] is False
    assert calls["n"] == 1
    assert "認証" in result["error"] or "401" in result["error"]


def test_default_asr_post_400_and_403_also_fail_without_retry(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    for code in (400, 403):
        calls = {"n": 0}

        def fake_post(url, path, api_key, timeout_sec=60, _code=code):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, _code, "err", None, None)

        result = ref.default_asr_post("/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: None)
        assert result["ok"] is False
        assert calls["n"] == 1


def test_default_asr_post_402_returns_japanese_balance_message(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")

    def fake_post(url, path, api_key, timeout_sec=60):
        raise urllib.error.HTTPError(url, 402, "Payment Required", None, None)

    result = ref.default_asr_post("/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: None)
    assert result["ok"] is False
    assert result["error"] == "Fish Audioの残高不足です"


def test_default_asr_post_5xx_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    calls = {"n": 0}
    sleep_calls = []

    def fake_post(url, path, api_key, timeout_sec=60):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError(url, 500, "err", None, None)
        return json.dumps({"text": "ok", "duration": 1.0, "segments": []}).encode("utf-8")

    result = ref.default_asr_post(
        "/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: sleep_calls.append(s)
    )
    assert result["ok"] is True
    assert calls["n"] == 2
    assert sleep_calls == [ref._FISH_ASR_RETRY_BACKOFF_SEC[0]]


def test_default_asr_post_5xx_exhausts_retries_and_fails(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    calls = {"n": 0}
    sleep_calls = []

    def fake_post(url, path, api_key, timeout_sec=60):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 503, "err", None, None)

    result = ref.default_asr_post(
        "/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: sleep_calls.append(s)
    )
    assert result["ok"] is False
    assert calls["n"] == 1 + ref._FISH_ASR_MAX_RETRIES
    assert sleep_calls == ref._FISH_ASR_RETRY_BACKOFF_SEC


def test_default_asr_post_connection_error_retries_via_oserror(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    calls = {"n": 0}
    sleep_calls = []

    def fake_post(url, path, api_key, timeout_sec=60):
        calls["n"] += 1
        raise OSError("connection reset")

    result = ref.default_asr_post(
        "/tmp/a.m4a", {}, _http_post=fake_post, _sleep=lambda s: sleep_calls.append(s)
    )
    assert result["ok"] is False
    assert calls["n"] == 1 + ref._FISH_ASR_MAX_RETRIES
    assert "接続" in result["error"]


def test_default_asr_post_custom_timeout_sec_passed_through(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "dummy-key")
    captured = []

    def fake_post(url, path, api_key, timeout_sec=60):
        captured.append(timeout_sec)
        return json.dumps({"text": "ok", "duration": 1.0, "segments": []}).encode("utf-8")

    ref.default_asr_post("/tmp/a.m4a", {"reference": {"asr_timeout_sec": 15}}, _http_post=fake_post)
    assert captured == [15]


# ---------------------------------------------------------------------------
# validate_reference_spec
# ---------------------------------------------------------------------------

_VALID_RHYTHM = {
    "sentence_count": 5, "avg_sentence_len": 12.0, "max_sentence_len": 24,
    "tone": "カジュアル", "endings": ["〜です", "〜ます"],
}


def test_validate_reference_spec_rejects_empty_beats():
    ok, errors, spec = ref.validate_reference_spec(
        {"beats": [], "rhythm": _VALID_RHYTHM}, "て" * 40, [], 10.0, "https://x.com/a"
    )
    assert ok is False
    assert spec is None
    assert any("beats" in e for e in errors)


def test_validate_reference_spec_rejects_invalid_role():
    data = {"beats": [{"role": "intro", "start": 0, "end": 2, "text": "t"}], "rhythm": _VALID_RHYTHM}
    ok, errors, spec = ref.validate_reference_spec(data, "て" * 40, [], 10.0, "https://x.com/a")
    assert ok is False
    assert any("role" in e for e in errors)


def test_validate_reference_spec_rejects_end_before_start():
    data = {"beats": [{"role": "hook", "start": 5, "end": 2, "text": "t"}], "rhythm": _VALID_RHYTHM}
    ok, errors, spec = ref.validate_reference_spec(data, "て" * 40, [], 10.0, "https://x.com/a")
    assert ok is False


def test_validate_reference_spec_requires_rhythm_keys():
    data = {"beats": [{"role": "hook", "start": 0, "end": 2, "text": "t"}], "rhythm": {"tone": "x"}}
    ok, errors, spec = ref.validate_reference_spec(data, "て" * 40, [], 10.0, "https://x.com/a")
    assert ok is False
    assert any("rhythm" in e for e in errors)


def test_validate_reference_spec_rejects_short_transcript():
    data = {"beats": [{"role": "hook", "start": 0, "end": 2, "text": "t"}], "rhythm": _VALID_RHYTHM}
    ok, errors, spec = ref.validate_reference_spec(data, "短い文字起こし", [], 10.0, "https://x.com/a")
    assert ok is False
    assert any("transcript" in e for e in errors)


def test_validate_reference_spec_success_builds_full_spec():
    data = {
        "beats": [
            {"role": "hook", "start": 0, "end": 3, "text": "hook text", "summary": "s1"},
            {"role": "cta", "start": 3, "end": 10, "text": "cta text", "summary": "s2"},
        ],
        "rhythm": _VALID_RHYTHM,
    }
    transcript = "て" * 40
    ok, errors, spec = ref.validate_reference_spec(data, transcript, [{"text": "a", "start": 0, "end": 1}], 10.0, "https://x.com/a")
    assert ok is True
    assert errors == []
    assert spec["version"] == 1
    assert spec["url"] == "https://x.com/a"
    assert spec["source"] == "fish_asr"
    assert spec["duration_sec"] == 10.0
    assert spec["transcript"] == transcript
    assert len(spec["beats"]) == 2
    assert spec["rhythm"]["tone"] == "カジュアル"
    assert spec["telops"] is None
    assert spec["cuts"] is None
    assert spec["warnings"] == []
    assert "fetched_at" in spec


# ---------------------------------------------------------------------------
# find_verbatim_overlap
# ---------------------------------------------------------------------------

def test_find_verbatim_overlap_below_min_len_is_not_flagged():
    text = "あ" * 14
    assert ref.find_verbatim_overlap(text, text) == []


def test_find_verbatim_overlap_at_min_len_is_flagged():
    text = "あ" * 15
    overlaps = ref.find_verbatim_overlap(text, text)
    assert overlaps == ["あ" * 15]


def test_find_verbatim_overlap_ignores_punctuation_and_whitespace():
    reference_text = "これは、とても大事なポイントです。しっかり覚えてください。"
    script_text = "これは とても大事なポイントです しっかり覚えてください"
    overlaps = ref.find_verbatim_overlap(reference_text, script_text, min_len=10)
    assert len(overlaps) >= 1


def test_find_verbatim_overlap_empty_when_no_overlap():
    assert ref.find_verbatim_overlap("全く違う文章です", "これも全然違う内容です", min_len=15) == []


def test_find_verbatim_overlap_empty_inputs_return_empty_list():
    assert ref.find_verbatim_overlap("", "何か") == []
    assert ref.find_verbatim_overlap("何か", "") == []
    assert ref.find_verbatim_overlap(None, None) == []


def test_find_verbatim_overlap_detects_across_fullwidth_halfwidth_variants():
    # NFKC正規化前は「１５」(全角数字)と「15」(半角数字)が別文字列として扱われ、
    # 丸写し検出をすり抜けてしまう。正規化後は一致として検出されるべき。
    reference_text = "今日は１５字ぴったりの文章で丸写しテストします"
    script_text = "今日は15字ぴったりの文章で丸写しテストします"
    overlaps = ref.find_verbatim_overlap(reference_text, script_text, min_len=15)
    assert len(overlaps) >= 1
    assert sum(len(o) for o in overlaps) >= 15


# ---------------------------------------------------------------------------
# build_reference_analysis_prompt
# ---------------------------------------------------------------------------

def test_build_reference_analysis_prompt_substitutes_placeholders():
    prompt = ref.build_reference_analysis_prompt("文字起こし本文", [{"text": "a", "start": 0, "end": 1}], 12.5)
    assert "文字起こし本文" in prompt
    assert "12.5" in prompt
    assert '"start": 0' in prompt or "start" in prompt
    assert "{TRANSCRIPT}" not in prompt
    assert "{SEGMENTS_JSON}" not in prompt
    assert "{DURATION}" not in prompt


# ---------------------------------------------------------------------------
# analyze_reference: エラー経路
# ---------------------------------------------------------------------------

def test_analyze_reference_returns_error_when_fetch_audio_fails(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)

    def failing_fetch(u, c):
        raise RuntimeError("ダウンロード失敗のテスト")

    result = ref.analyze_reference("https://x.com/e1", cfg=cfg, fetch_audio=failing_fetch)
    assert result["ok"] is False
    assert "音声取得" in result["error"]


def test_analyze_reference_returns_error_when_asr_fails(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 10.0}

    def failing_asr(path, c):
        return {"ok": False, "error": "文字起こしのテストエラー"}

    result = ref.analyze_reference("https://x.com/e2", cfg=cfg, fetch_audio=fake_fetch, asr_post=failing_asr)
    assert result["ok"] is False
    assert result["error"] == "文字起こしのテストエラー"


def test_analyze_reference_returns_error_when_claude_call_unavailable(tmp_path, monkeypatch):
    # claude_call未指定(None)時はモジュール変数call_claude_jsonにフォールバックする仕様なので、
    # 「claude呼び出しが利用不可」を再現するにはモジュール変数自体をNoneにする
    # （claude_runner未整備環境でのimport失敗と同じ状態）。
    monkeypatch.setattr(ref, "call_claude_json", None)
    cfg = _cfg_with_cache_dir(tmp_path)

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 10.0}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 10.0, "segments": []}

    result = ref.analyze_reference("https://x.com/e3", cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr)
    assert result["ok"] is False
    assert "構成解析" in result["error"]


def test_analyze_reference_returns_error_when_claude_response_invalid(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 10.0}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 10.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {"ok": False, "data": None, "error": "claude呼び出し失敗のテスト"}

    result = ref.analyze_reference(
        "https://x.com/e4", cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude
    )
    assert result["ok"] is False
    assert result["error"] == "claude呼び出し失敗のテスト"


def test_analyze_reference_invalid_url_returns_error_without_calling_anything():
    def _boom(*args, **kwargs):
        raise AssertionError("不正URL時は外部呼び出しが発生してはいけない")

    result = ref.analyze_reference("", fetch_audio=_boom, asr_post=_boom, claude_call=_boom)
    assert result["ok"] is False
    assert result["spec"] is None


def test_analyze_reference_cleans_tmp_dir_after_success(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    fetch_tmp_dir = tmp_path / "href_reference_xyz"
    fetch_tmp_dir.mkdir()
    audio_path = fetch_tmp_dir / "audio.m4a"
    audio_path.write_bytes(b"dummy")

    def fake_fetch(u, c):
        return {"path": str(audio_path), "duration_sec": 10.0, "tmp_dir": str(fetch_tmp_dir)}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 10.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {
                "beats": [{"role": "hook", "start": 0, "end": 10, "text": "t", "summary": "s"}],
                "rhythm": _VALID_RHYTHM,
            },
            "model_used": "m",
        }

    result = ref.analyze_reference(
        "https://x.com/tmp1", cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude
    )
    assert result["ok"] is True
    assert not os.path.exists(str(fetch_tmp_dir))


def test_analyze_reference_cleans_tmp_dir_after_asr_failure(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    fetch_tmp_dir = tmp_path / "href_reference_fail"
    fetch_tmp_dir.mkdir()
    audio_path = fetch_tmp_dir / "audio.m4a"
    audio_path.write_bytes(b"dummy")

    def fake_fetch(u, c):
        return {"path": str(audio_path), "duration_sec": 10.0, "tmp_dir": str(fetch_tmp_dir)}

    def failing_asr(path, c):
        return {"ok": False, "error": "文字起こしのテストエラー"}

    result = ref.analyze_reference("https://x.com/tmp2", cfg=cfg, fetch_audio=fake_fetch, asr_post=failing_asr)
    assert result["ok"] is False
    assert not os.path.exists(str(fetch_tmp_dir))


def test_analyze_reference_does_not_delete_unowned_tmp_dir_from_custom_fetch(tmp_path):
    # 回帰(codex-review指摘): カスタムfetch_audio(DI)がhref_reference_接頭辞を持たない
    # 任意のディレクトリをtmp_dirとして返しても、analyze_referenceはそれを丸ごと
    # rmtreeしてはいけない(呼び出し元が管理する共有ディレクトリを誤削除する恐れがあるため)。
    # 音声ファイル単体のみ削除されることを確認する。
    cfg = _cfg_with_cache_dir(tmp_path)
    shared_dir = tmp_path / "some_shared_project_dir"
    shared_dir.mkdir()
    audio_path = shared_dir / "audio.m4a"
    audio_path.write_bytes(b"dummy")
    sibling_file = shared_dir / "keep_me.txt"
    sibling_file.write_text("do not delete", encoding="utf-8")

    def fake_fetch(u, c):
        return {"path": str(audio_path), "duration_sec": 10.0, "tmp_dir": str(shared_dir)}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 10.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {
                "beats": [{"role": "hook", "start": 0, "end": 10, "text": "t", "summary": "s"}],
                "rhythm": _VALID_RHYTHM,
            },
            "model_used": "m",
        }

    result = ref.analyze_reference(
        "https://x.com/tmp3", cfg=cfg, fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude
    )
    assert result["ok"] is True
    assert shared_dir.exists()
    assert sibling_file.exists()
    assert not audio_path.exists()  # 音声ファイル自体は削除される


def test_analyze_reference_progress_callback_reports_expected_stages(tmp_path):
    cfg = _cfg_with_cache_dir(tmp_path)
    events = []

    def fake_fetch(u, c):
        return {"path": "/tmp/dummy.m4a", "duration_sec": 10.0}

    def fake_asr(path, c):
        return {"ok": True, "text": "て" * 40, "duration": 10.0, "segments": []}

    def fake_claude(prompt, timeout_sec=600):
        return {
            "ok": True,
            "data": {
                "beats": [{"role": "hook", "start": 0, "end": 10, "text": "t", "summary": "s"}],
                "rhythm": _VALID_RHYTHM,
            },
            "model_used": "m",
        }

    result = ref.analyze_reference(
        "https://x.com/e5", cfg=cfg, progress_cb=lambda stage, detail=None: events.append(stage),
        fetch_audio=fake_fetch, asr_post=fake_asr, claude_call=fake_claude,
    )
    assert result["ok"] is True
    assert "download" in events
    assert "asr" in events
    assert "analyze" in events
    assert "done" in events
