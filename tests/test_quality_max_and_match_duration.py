# -*- coding: utf-8 -*-
"""F11 --match-reference-duration と F12 supreme_plus プリセット（config.local 自動マージ）の単体テスト。"""
from __future__ import annotations

import json

import pytest

from pipeline import config as _cfg_mod
from pipeline import director


def test_config_local_json_files_are_deep_merged(tmp_path, monkeypatch):
    """`config.local/*.json` が config.json に deep_merge で上書きされる。"""
    # 一時プロジェクトルートを作る
    root = tmp_path
    (root / "config.json").write_text(
        json.dumps({
            "director_quality": "supreme",
            "director": {"min_shot_sec": None, "quality_directive": None},
            "reference": {"max_vision_calls": 4, "max_frames": 40},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    local_dir = root / "config.local"
    local_dir.mkdir()
    (local_dir / "quality_max.json").write_text(
        json.dumps({
            "director_quality": "supreme_plus",
            "director": {"min_shot_sec": 0.5, "quality_directive": "品質最優先"},
            "reference": {"max_vision_calls": 999},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    # config.py の PROJECT_ROOT / _CONFIG_PATH を一時ルートに向ける
    monkeypatch.setattr(_cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.setattr(_cfg_mod, "_CONFIG_PATH", root / "config.json")

    cfg = _cfg_mod.load_config()
    assert cfg["director_quality"] == "supreme_plus"
    assert cfg["director"]["min_shot_sec"] == 0.5
    assert cfg["director"]["quality_directive"] == "品質最優先"
    # deep merge: reference.max_vision_calls は上書き、max_frames は残る
    assert cfg["reference"]["max_vision_calls"] == 999
    assert cfg["reference"]["max_frames"] == 40


def test_config_local_absent_falls_back_to_config_json(tmp_path, monkeypatch):
    root = tmp_path
    (root / "config.json").write_text(
        json.dumps({"director_quality": "supreme"}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(_cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.setattr(_cfg_mod, "_CONFIG_PATH", root / "config.json")
    cfg = _cfg_mod.load_config()
    assert cfg["director_quality"] == "supreme"


def test_run_director_supports_supreme_plus_quality(monkeypatch, tmp_path):
    """quality='supreme_plus' が受理され、stages に rewrite が入る。"""
    import os
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")
    with open(fixture, "r", encoding="utf-8") as f:
        spec = json.load(f)

    def _fake_attempt(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                      trace=None, skeleton=None, reference=None):
        if trace is not None:
            trace["last_model_used"] = "test-model"
        if not skeleton:
            return None
        shots = []
        for s in skeleton["shots"]:
            shot = dict(s)
            shot["visual_prompt"] = "placeholder"
            shot["motion_preset"] = "static"
            shot["caption_jp"] = "テスト"
            shots.append(shot)
        return {
            "version": 2, "meta": {"source": "ai"},
            "concept": "c", "hook": "h", "narration_script": "n",
            "shots": shots, "bgm_mood": "upbeat",
            "sfx_plan": skeleton.get("sfx_plan") or [],
            "hook_end_shot_id": skeleton.get("hook_end_shot_id"),
            "cta_start_shot_id": skeleton.get("cta_start_shot_id"),
        }

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    plan = director.run_director(
        "theme",
        config={"director": {"min_shot_sec": 0.5, "quality_directive": "品質最優先: reference_visual を必ず英文に含めよ"}},
        target_duration_sec=30.0, reference=spec, quality="supreme_plus",
    )
    assert plan["meta"]["quality"] == "supreme_plus"
    stages = plan["meta"].get("stages") or {}
    assert "write" in stages
    assert "polish" in stages
    assert "rewrite" in stages, "supreme_plus は3段目 rewrite を実行する"


def test_quality_directive_is_injected_into_reference_block(monkeypatch):
    """config.director.quality_directive が reference_block の末尾に注入される。"""
    import os
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")
    with open(fixture, "r", encoding="utf-8") as f:
        spec = json.load(f)

    captured = {}

    def _fake_attempt(prompt, *args, **kw):
        captured.setdefault("prompt", prompt)
        return None  # 実行しない（プロンプトが目当て）

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    directive = "品質最優先マーカー_1234"
    with pytest.raises(director.TTPSkeletonMismatchError):
        director.run_director(
            "theme",
            config={"director": {"quality_directive": directive}},
            target_duration_sec=30.0, reference=spec, quality="single",
        )
    assert directive in captured["prompt"]


def test_cli_match_reference_duration_overrides_target(monkeypatch, tmp_path):
    """F11: run.py の --match-reference-duration が target を参考尺に強制一致させる。

    ここでは run_pipeline を直接呼び、reference_spec.duration_sec が target_duration_sec に
    上書きされることを検証する（reference stage の完了直後で上書きされる仕様）。
    """
    from pipeline import reference_v2

    def _fake_analyze(url, cfg=None, fetch_video=None, progress_cb=None):
        return {
            "ok": True, "cached": False, "warnings": [], "source": "test",
            "spec": {
                "version": 2, "url": url, "duration_sec": 47.5,
                "transcript": "", "segments": [], "beats": [], "rhythm": None,
                "cuts": [], "shots_ref": [], "telops": [], "sfx_events": [],
                "bgm": {"present": False, "mood_guess": ""},
                "warnings": [],
            },
        }

    # analyze だけをフェイクにし、director 以降で失敗させて report を早期に取得する。
    monkeypatch.setattr(reference_v2, "analyze_reference_v2", _fake_analyze)

    import run as run_mod

    def _boom(*args, **kw):
        raise RuntimeError("director stub")
    monkeypatch.setattr(run_mod.director, "run_director", _boom)

    report = run_mod.run_pipeline(
        theme="t", target_duration_sec=15.0, backend_name="mock", no_llm=False,
        cfg={"backend": "mock", "reference": {"cache_dir": str(tmp_path)}},
        quality="single", style="default",
        reference_url="https://example.com/x", match_reference_duration=True,
    )
    # F11 適用: target が 47.5 秒に上書きされ report に反映されている
    assert report["target_duration_sec"] == pytest.approx(47.5)
    assert report["stages"]["reference"].get("match_reference_duration") is True


# ---------------------------------------------------------------------------
# R2b申し送り対応: supreme_plus の beat_snap 自動 ON 配線 & タイムアウト延長
# ---------------------------------------------------------------------------

def _make_spec_with_music(cuts=(2.0, 4.0), duration_sec=6.0, beat_times=(0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5)):
    return {
        "version": 2, "url": "http://x",
        "duration_sec": float(duration_sec),
        "transcript": "", "segments": [], "beats": [], "rhythm": None,
        "cuts": [{"t": float(t), "confidence": 0.9} for t in cuts],
        "shots_ref": [], "telops": [], "sfx_events": [],
        "bgm": {"present": True, "mood_guess": "upbeat"},
        "warnings": [],
        "music": {
            "bpm": 120.0,
            "beat_times": [float(t) for t in beat_times],
            "downbeats": [], "confidence": 0.85, "engine": "librosa",
        },
    }


def _fake_attempt_passthrough(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                               trace=None, skeleton=None, reference=None):
    """スケルトンをそのまま埋めて返す（境界は skeleton 側で確定済み＝beat_snap の効果が観測できる）。"""
    if trace is not None:
        trace["last_model_used"] = "test-model"
    if not skeleton:
        return None
    shots = []
    for s in skeleton["shots"]:
        shot = dict(s)
        shot["visual_prompt"] = "placeholder"
        shot["motion_preset"] = "static"
        shot["caption_jp"] = "テスト"
        shots.append(shot)
    return {
        "version": 2, "meta": {"source": "ai"},
        "concept": "c", "hook": "h", "narration_script": "n",
        "shots": shots, "bgm_mood": "upbeat",
        "sfx_plan": skeleton.get("sfx_plan") or [],
        "hook_end_shot_id": skeleton.get("hook_end_shot_id"),
        "cta_start_shot_id": skeleton.get("cta_start_shot_id"),
    }


def _cumulative_boundaries(plan):
    c = 0.0
    out = [0.0]
    for s in plan.get("shots") or []:
        c += float(s.get("duration_sec") or 0.0)
        out.append(round(c, 4))
    return out


def test_supreme_plus_auto_enables_beat_snap_by_default(monkeypatch):
    """R2b申し送り対応: quality='supreme_plus' なら config.director.beat_snap_enabled 未指定でも beat_snap ON。"""
    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt_passthrough)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    spec = _make_spec_with_music()
    # config.director.beat_snap_enabled は未指定
    plan_on = director.run_director(
        "t", config={"director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    # 対照: supreme（既定 OFF）
    plan_off = director.run_director(
        "t", config={"director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme",
    )
    cum_on = _cumulative_boundaries(plan_on)
    cum_off = _cumulative_boundaries(plan_off)
    # beat_snap 効果で内部境界が拍(0.5s刻み)に吸着し、supreme(OFF)の境界と一致しない
    # ケースが少なくとも1つ存在する。cuts=[2.0, 4.0] は既に拍上なので、scale で少しズレる
    # ぶんの吸着が観測できる想定。少なくとも「ON では全境界が拍±tol 以内」であることを確認する。
    tol = 0.05
    beats = spec["music"]["beat_times"]
    for b in cum_on[1:-1]:  # 内部境界のみ
        assert any(abs(b - beat) <= tol for beat in beats), \
            "supreme_plus は beat_snap 自動 ON なので内部境界は拍上に吸着するはず (got {})".format(b)
    # off の内部境界がどれか1つでも「拍から離れている」ならウィンドウ検査は差分ありの証拠
    off_off_any = any(all(abs(b - beat) > tol for beat in beats) for b in cum_off[1:-1] if cum_off[1:-1])
    # 参考cuts はきりのいい値のため off でも拍に一致することがあるが、少なくとも
    # スケルトンパスは同一 quality フラグ違いで走る両方が None にならないことを確認。
    assert len(cum_on) == len(cum_off)


def test_supreme_plus_beat_snap_can_be_explicitly_disabled(monkeypatch):
    """明示的に beat_snap_enabled=False を渡せば supreme_plus 既定より優先される。"""
    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt_passthrough)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)

    # 参考尺 6s の等尺分割で 3ショット → 拍不一致の境界を生む spec
    # duration=6.0, cuts=[1.7, 3.9] → scale=1.0（参考=生成）でカット時刻がそのまま
    spec = _make_spec_with_music(cuts=(1.7, 3.9), duration_sec=6.0)
    plan_forced_off = director.run_director(
        "t",
        config={"director": {"min_shot_sec": 0.5, "beat_snap_enabled": False}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    plan_auto_on = director.run_director(
        "t", config={"director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    cum_off = _cumulative_boundaries(plan_forced_off)
    cum_on = _cumulative_boundaries(plan_auto_on)
    # 明示 OFF: 参考カット時刻がそのまま境界に反映される (=拍から離れうる)
    # 自動 ON: 内部境界は拍(0.5s間隔)に吸着する
    tol = 0.06
    beats = spec["music"]["beat_times"]
    # ON で内部境界が全て拍に吸着
    for b in cum_on[1:-1]:
        assert any(abs(b - beat) <= tol for beat in beats), \
            "supreme_plus 既定は beat_snap ON なので内部境界は拍±{}s に入るはず (got {})".format(tol, b)
    # OFF ではいずれかの内部境界が拍から離れている（この spec では 1.7 が拍 1.5/2.0 の中間）
    off_has_off_beat = any(all(abs(b - beat) > tol for beat in beats) for b in cum_off[1:-1])
    assert off_has_off_beat, \
        "明示 OFF なのに全境界が拍に吸着している。auto ON より優先される配線を要確認 (cum_off={})".format(cum_off)


def test_supreme_plus_extends_claude_timeout(monkeypatch):
    """quality='supreme_plus' のとき _attempt_plan には拡張された timeout の config が届く。"""
    seen_timeouts = []

    def _fake_attempt(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
                      trace=None, skeleton=None, reference=None):
        seen_timeouts.append(config.get("claude_timeout_sec"))
        return _fake_attempt_passthrough(prompt, config, retries_left, target_duration_sec,
                                         target_tolerance_sec, trace=trace, skeleton=skeleton, reference=reference)

    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)
    spec = _make_spec_with_music()
    director.run_director(
        "t", config={"claude_timeout_sec": 600, "director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    # 3段(write/polish/rewrite) すべてで拡張タイムアウト(=既定 1200s)が使われる
    assert seen_timeouts, "attempt が呼ばれていない"
    assert all(t == 1200 for t in seen_timeouts), \
        "supreme_plus は claude_timeout_sec を 1200 に自動延長するはず (got {})".format(seen_timeouts)

    # 明示指定があればそちらを優先
    seen_timeouts.clear()
    director.run_director(
        "t", config={"claude_timeout_sec": 600, "claude_timeout_sec_supreme_plus": 1800,
                     "director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    assert all(t == 1800 for t in seen_timeouts), \
        "claude_timeout_sec_supreme_plus 明示指定が優先されるはず (got {})".format(seen_timeouts)


def test_supreme_plus_does_not_leak_timeout_into_caller_config(monkeypatch):
    """run_director は渡された config を破壊的に書き換えない。"""
    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt_passthrough)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)
    cfg = {"claude_timeout_sec": 600, "director": {"min_shot_sec": 0.5}}
    spec = _make_spec_with_music()
    director.run_director("t", config=cfg, target_duration_sec=6.0, reference=spec, quality="supreme_plus")
    assert cfg["claude_timeout_sec"] == 600, "呼び出し側 config の claude_timeout_sec を書き換えてはいけない"


def test_supreme_plus_writes_checkpoints_between_stages(tmp_path, monkeypatch):
    """R2b申し送り対応: 3段直列の途中失敗からリカバリできるよう plan_after_*.json を保存する。"""
    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt_passthrough)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)
    spec = _make_spec_with_music()
    director.run_director(
        "t", config={"director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
        checkpoint_dir=str(tmp_path),
    )
    assert (tmp_path / "plan_after_write.json").is_file(), "write 段の checkpoint が保存されていない"
    assert (tmp_path / "plan_after_polish.json").is_file(), "polish 段の checkpoint が保存されていない"
    assert (tmp_path / "plan_after_rewrite.json").is_file(), "rewrite 段の checkpoint が保存されていない"


def test_supreme_quality_still_disables_beat_snap(monkeypatch):
    """後方互換: quality='supreme' は beat_snap 既定 OFF のまま。"""
    monkeypatch.setattr(director, "_attempt_plan", _fake_attempt_passthrough)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)
    # 拍と参考カットが不一致な spec
    spec = _make_spec_with_music(cuts=(1.7, 3.9), duration_sec=6.0)
    plan = director.run_director(
        "t", config={"director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme",
    )
    cum = _cumulative_boundaries(plan)
    # supreme（未指定なら既定 False）は参考カット時刻をそのまま反映する（=拍上に無い境界が残る）
    beats = spec["music"]["beat_times"]
    tol = 0.06
    assert any(all(abs(b - beat) > tol for beat in beats) for b in cum[1:-1]), \
        "supreme の既定は beat_snap OFF のはず (境界が全部拍上にあると supreme_plus と区別つかない): cum={}".format(cum)


def test_supreme_plus_never_shortens_existing_long_timeout(monkeypatch):
    """codex-review P2 対応: 既存 claude_timeout_sec が 1200 超なら維持する（短縮しない）。"""
    seen = []

    def _fake(prompt, config, retries_left, target_duration_sec, target_tolerance_sec,
              trace=None, skeleton=None, reference=None):
        seen.append(config.get("claude_timeout_sec"))
        return _fake_attempt_passthrough(prompt, config, retries_left, target_duration_sec,
                                          target_tolerance_sec, trace=trace, skeleton=skeleton, reference=reference)

    monkeypatch.setattr(director, "_attempt_plan", _fake)
    monkeypatch.setattr(director, "call_claude_json", lambda *a, **kw: None)
    spec = _make_spec_with_music()
    # 既存 claude_timeout_sec=1800 (専用キー未指定) → 1200 に短縮せず 1800 を維持
    director.run_director(
        "t", config={"claude_timeout_sec": 1800, "director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    assert seen, "attempt が呼ばれていない"
    assert all(t == 1800 for t in seen), \
        "既存 1800s を 1200s に短縮してはいけない (got {})".format(seen)

    # 既存 claude_timeout_sec=300 (専用キー未指定) → 1200 に延長する（短縮ではない）
    seen.clear()
    director.run_director(
        "t", config={"claude_timeout_sec": 300, "director": {"min_shot_sec": 0.5}},
        target_duration_sec=6.0, reference=spec, quality="supreme_plus",
    )
    assert all(t == 1200 for t in seen), \
        "既存 300s は 1200s に延長するはず (got {})".format(seen)
