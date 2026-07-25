# -*- coding: utf-8 -*-
"""0円保証（むりょうコース）の機械検証。

plan_tier="free" のとき、Higgsfield（映像）/ Fish Audio（TTS/ASR）の外部（課金）コードパスへ
一切入らないことを、各切替点（backend / tts / asr）で個別に検証する。この3点は
pipeline.plan_tier と pipeline.reference_v2.resolve_asr_post に集約されている。
"""
import pipeline.plan_tier as pt
from pipeline.config import load_config
from pipeline.visual import get_backend
from pipeline.visual.higgsfield_backend import HiggsfieldBackend
from pipeline import tts as tts_mod
from pipeline import reference_v2


# --- 1) 映像backend: free は必ず mock、Higgsfield を構築しない -------------------

def test_free_backend_is_mock_never_higgsfield():
    backend_name = pt.resolve_backend("free", "higgsfield")
    assert backend_name == "mock"
    backend = get_backend(backend_name, load_config())
    assert backend.name == "mock"
    assert not isinstance(backend, HiggsfieldBackend)


def test_paid_backend_resolves_to_higgsfield_name():
    assert pt.resolve_backend("paid", "mock") == "higgsfield"


# --- 2) TTS: free は say、Fish Audio TTS を選ばない ----------------------------

def test_free_tts_is_say_not_fish():
    cfg = pt.apply_tier_to_cfg(load_config(), "free")
    backend = tts_mod.get_tts_backend(voice="Kyoko", cfg=cfg)
    assert isinstance(backend, tts_mod.SayTTSBackend)
    assert not isinstance(backend, tts_mod.FishAudioTTSBackend)


def test_paid_tts_is_fish():
    cfg = pt.apply_tier_to_cfg(load_config(), "paid")
    backend = tts_mod.get_tts_backend(voice="Kyoko", cfg=cfg)
    assert isinstance(backend, tts_mod.FishAudioTTSBackend)


# --- 3) ASR: free は外部 Fish Audio ASR を呼ばない ------------------------------

def test_free_asr_post_is_replaced_with_noop_even_if_spy_passed():
    calls = {"n": 0}

    def _spy_asr(audio_path, cfg=None, **kw):
        calls["n"] += 1
        return {"ok": True, "text": "should-not-be-called"}

    cfg_free = pt.apply_tier_to_cfg(load_config(), "free")
    resolved = reference_v2.resolve_asr_post(_spy_asr, cfg_free)
    # free のとき、明示 spy を渡しても no-op へ差し替わる（外部呼び出しゼロ）。
    assert resolved is not _spy_asr
    result = resolved("/tmp/whatever.wav", cfg_free)
    assert result["ok"] is False
    assert calls["n"] == 0


def test_paid_asr_post_uses_provided_callable():
    def _spy_asr(audio_path, cfg=None, **kw):
        return {"ok": True}

    cfg_paid = pt.apply_tier_to_cfg(load_config(), "paid")
    resolved = reference_v2.resolve_asr_post(_spy_asr, cfg_paid)
    assert resolved is _spy_asr


def test_no_tier_asr_post_is_passthrough():
    def _spy_asr(audio_path, cfg=None, **kw):
        return {"ok": True}

    resolved = reference_v2.resolve_asr_post(_spy_asr, load_config())
    assert resolved is _spy_asr
