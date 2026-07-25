# -*- coding: utf-8 -*-
"""pipeline/reference_v2.py: 話者推定 estimate_narrator_voice の分類ロジックと spec validate 追随。

librosa/audio は _librosa_np 差し替えで機械独立にする（実音声不要・決定論）。
実音声での実測値は別途 E2E ログで確認する（Kyoko=female/265Hz, Eddy=male/102Hz）。
"""
import numpy as np

from pipeline import reference_v2 as r2


class _FakeLibrosa:
    """librosa の最小モック。load は 2 秒の一定信号、pyin は与えた f0 列を返す。"""

    def __init__(self, f0_values):
        self._f0 = np.array(f0_values, dtype=float)

    def load(self, path, sr=16000, mono=True):
        return np.ones(int(sr * 2), dtype=float), sr

    def pyin(self, y, fmin=70.0, fmax=400.0, sr=16000):
        f0 = self._f0
        voiced = ~np.isnan(f0)
        return f0, voiced, None


def _seg(dur=2.0, text="あ" * 12):
    return [{"start": 0.0, "end": dur, "text": text}]


def _estimate(f0_values, segments=None, dur=2.0):
    return r2.estimate_narrator_voice(
        "x.wav", segments if segments is not None else _seg(dur), dur,
        _librosa_np=(_FakeLibrosa(f0_values), np),
    )


# --- 分類 ---

def test_female_high_pitch():
    nv = _estimate([250.0] * 60)
    assert nv["gender_guess"] == "female"
    assert nv["pitch"] == "high"
    assert nv["f0_median_hz"] == 250.0
    assert nv["confidence"] >= 0.6


def test_female_mid_pitch():
    nv = _estimate([200.0] * 60)
    assert nv["gender_guess"] == "female" and nv["pitch"] == "mid"


def test_male_low_pitch():
    nv = _estimate([110.0] * 60)
    assert nv["gender_guess"] == "male" and nv["pitch"] == "low"


def test_ambiguous_band_is_unknown_low_conf():
    nv = _estimate([165.0] * 60)
    assert nv["gender_guess"] == "unknown"
    assert nv["confidence"] < 0.5


def test_speech_rate_cps_computed():
    nv = _estimate([220.0] * 60, segments=[{"start": 0.0, "end": 2.0, "text": "あ" * 20}])
    # 20 文字 / 2 秒 = 10.0 cps
    assert nv["speech_rate_cps"] == 10.0


def test_speech_rate_none_without_text():
    nv = _estimate([220.0] * 60, segments=[{"start": 0.0, "end": 2.0, "text": ""}])
    assert nv["speech_rate_cps"] is None


# --- ガード ---

def test_returns_none_when_librosa_missing():
    nv = r2.estimate_narrator_voice("x.wav", _seg(), 2.0, _librosa_np=(None, None))
    assert nv is None


def test_returns_none_without_segments():
    nv = r2.estimate_narrator_voice("x.wav", [], 2.0, _librosa_np=(_FakeLibrosa([220.0] * 60), np))
    assert nv is None


def test_returns_none_when_too_few_voiced_frames():
    # 有声フレームが 10 未満（大半が nan）
    f0 = [np.nan] * 60
    f0[0] = 220.0
    nv = _estimate(f0)
    assert nv is None


def test_confidence_discounted_by_low_voiced_ratio():
    # 半分が nan（有声率 0.5）→ ベース信頼から割り引かれる
    f0 = [220.0, np.nan] * 40
    nv = _estimate(f0)
    assert nv is not None and 0.1 <= nv["confidence"] <= 0.95


# --- validate 追随 ---

def _valid_base_spec():
    return {
        "version": 2, "url": "u", "duration_sec": 10.0, "transcript": "t",
        "segments": [], "beats": [], "rhythm": None, "cuts": [], "shots_ref": [],
        "telops": [], "sfx_events": [], "bgm": {}, "warnings": [],
    }


def test_validate_accepts_valid_narrator_voice():
    spec = _valid_base_spec()
    spec["narrator_voice"] = {"gender_guess": "female", "pitch": "mid", "confidence": 0.8}
    ok, errors, norm = r2.validate_reference_spec_v2(spec)
    assert ok, errors
    assert norm["narrator_voice"]["gender_guess"] == "female"


def test_validate_rejects_bad_gender():
    spec = _valid_base_spec()
    spec["narrator_voice"] = {"gender_guess": "robot"}
    ok, errors, _ = r2.validate_reference_spec_v2(spec)
    assert not ok
    assert any("gender_guess" in e for e in errors)


def test_validate_rejects_bad_pitch():
    spec = _valid_base_spec()
    spec["narrator_voice"] = {"pitch": "ultrahigh"}
    ok, errors, _ = r2.validate_reference_spec_v2(spec)
    assert not ok
    assert any("pitch" in e for e in errors)


def test_validate_allows_absent_narrator_voice():
    ok, errors, norm = r2.validate_reference_spec_v2(_valid_base_spec())
    assert ok, errors
    assert "narrator_voice" not in norm
