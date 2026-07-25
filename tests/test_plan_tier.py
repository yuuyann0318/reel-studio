# -*- coding: utf-8 -*-
"""pipeline/plan_tier.py の単体テスト（プラン=ワンセットの1変数集約ロジック）。"""
import pipeline.plan_tier as pt


def test_resolve_backend_free_forces_mock_over_request():
    # free は backend 個別指定より優先し、必ず mock（Higgsfield を構築させない=0円保証の要）。
    assert pt.resolve_backend("free") == "mock"
    assert pt.resolve_backend("free", "higgsfield") == "mock"
    assert pt.resolve_backend("free", "cloudapi") == "mock"


def test_resolve_backend_paid_forces_higgsfield():
    assert pt.resolve_backend("paid") == "higgsfield"
    assert pt.resolve_backend("paid", "mock") == "higgsfield"


def test_resolve_backend_no_tier_respects_request_backward_compat():
    assert pt.resolve_backend(None, "higgsfield") == "higgsfield"
    assert pt.resolve_backend(None, "cloudapi") == "cloudapi"
    assert pt.resolve_backend(None, None) == "mock"
    assert pt.resolve_backend("bogus", "higgsfield") == "higgsfield"


def test_normalize_tier():
    assert pt.normalize_tier("free") == "free"
    assert pt.normalize_tier("PAID") == "paid"
    assert pt.normalize_tier("  free ") == "free"
    assert pt.normalize_tier("bogus") is None
    assert pt.normalize_tier(None) is None
    assert pt.normalize_tier(123) is None


def test_infer_tier_backward_compat_from_backend():
    assert pt.infer_tier("free", "higgsfield") == "free"  # 明示 tier 優先
    assert pt.infer_tier(None, "mock") == "free"
    assert pt.infer_tier(None, "higgsfield") == "paid"
    assert pt.infer_tier(None, "cloudapi") == "paid"
    assert pt.infer_tier(None, None) == "free"


def test_apply_tier_to_cfg_free_disables_paid_apis():
    cfg = {"tts": {"engine": "fish_audio", "fish_audio": {"model": "x"}}, "reference": {"cache_dir": "c"}}
    out = pt.apply_tier_to_cfg(cfg, "free")
    assert out["tts"]["engine"] == "say"
    assert out["reference"]["asr_enabled"] is False
    # 元 cfg は破壊しない（deepcopy）。
    assert cfg["tts"]["engine"] == "fish_audio"
    assert "asr_enabled" not in cfg["reference"]
    # 既存キーは温存する。
    assert out["tts"]["fish_audio"]["model"] == "x"
    assert out["reference"]["cache_dir"] == "c"


def test_apply_tier_to_cfg_paid_enables_paid_apis():
    out = pt.apply_tier_to_cfg({"tts": {}, "reference": {}}, "paid")
    assert out["tts"]["engine"] == "fish_audio"
    assert out["reference"]["asr_enabled"] is True


def test_apply_tier_to_cfg_no_tier_is_passthrough_copy():
    cfg = {"tts": {"engine": "fish_audio"}, "reference": {}}
    out = pt.apply_tier_to_cfg(cfg, None)
    assert out == cfg
    assert out is not cfg  # コピー


def test_estimate_coins_free_is_zero():
    est = pt.estimate_coins({"higgsfield": {"max_credits_per_shot": 10}}, "free", duration_sec=30)
    assert est["coins"] == 0
    assert est["plan_tier"] == "free"
    assert est["approximate"] is False


def test_estimate_coins_paid_scales_with_duration_and_config():
    cfg = {"higgsfield": {"max_credits_per_shot": 12}}
    est = pt.estimate_coins(cfg, "paid", duration_sec=40)
    assert est["plan_tier"] == "paid"
    assert est["per_shot"] == 12
    assert est["shot_count"] == pt.estimate_shot_count(40)
    assert est["coins"] == est["shot_count"] * 12
    assert est["approximate"] is True


def test_estimate_shot_count_clamped():
    assert pt.estimate_shot_count(1) == pt._EST_MIN_SHOTS
    assert pt.estimate_shot_count(9999) == pt._EST_MAX_SHOTS
    assert pt.estimate_shot_count(0) >= pt._EST_MIN_SHOTS
    assert pt.estimate_shot_count(None) >= pt._EST_MIN_SHOTS
