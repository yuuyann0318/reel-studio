# -*- coding: utf-8 -*-
"""TTS実効性の機械検証: 選ばれた声Aと声Bで同一文を実生成し、出力音声の f0 中央値が
有意に異なることを確認する（＝「声が実際に変わっている」ことを機械的に保証する）。

- free tier (macOS `say`): Kyoko(女性) vs Otoya(男性) を必ず実合成する（無料・キー不要）。
  `say` バイナリが無い環境 or 声が入っていない環境ではスキップ。
- paid tier (Fish Audio): FISH_AUDIO_API_KEY があるときのみ実行する opt-in テスト。
  実APIを2回叩くため CI では通常スキップ。ローカル/検証ではスキップ理由を明示する。

f0中央値は pipeline/reference_v2.py と同じ librosa.pyin(fmin=70,fmax=400) で測る。
分類しきい値も同ファイルと共通（女性>=180Hz / 男性<=150Hz / あいまい150-180）。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from pipeline import tts
from pipeline.config import project_root


def _librosa_or_skip():
    try:
        import librosa  # noqa: F401
        import numpy as np  # noqa: F401
    except Exception as exc:
        pytest.skip("librosa/numpy 未導入のためスキップ: {}".format(exc))


def _f0_median_hz(wav_path):
    import librosa
    import numpy as np
    y, sr = librosa.load(wav_path, sr=16000, mono=True)
    # duration 短すぎ・完全無音は評価不能
    if y is None or len(y) < sr // 2:
        return None
    f0, _voiced, _ = librosa.pyin(y, fmin=70.0, fmax=400.0, sr=sr)
    voiced_f0 = f0[~np.isnan(f0)]
    if len(voiced_f0) < 10:
        return None
    return float(np.median(voiced_f0))


def _has_say_voice(name):
    if shutil.which("say") is None:
        return False
    try:
        proc = subprocess.run(["say", "-v", "?"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10)
    except Exception:
        return False
    listing = proc.stdout.decode("utf-8", "replace")
    return any(line.split()[0:1] == [name] for line in listing.splitlines() if line.strip())


def _cfg():
    return {
        "ffmpeg_bin": str(project_root() / "bin" / "ffmpeg"),
        "ffprobe_bin": str(project_root() / "bin" / "ffprobe"),
    }


_TEXT_JP = "こんにちは。今日はAIで副業を始める方法についてお話しします。"


_MALE_SAY_CANDIDATES = ("Otoya", "Eddy", "Reed", "Rocko", "Grandpa")


def _first_available_male_say():
    for name in _MALE_SAY_CANDIDATES:
        if _has_say_voice(name):
            return name
    return None


def test_free_tier_female_vs_male_say_f0_differ(tmp_path):
    """`say` の 女性声(Kyoko) と 男性声 の TTS 出力の f0 中央値は明確に差が出る。

    これは「声カタログで別の engine_voice_id を選ぶと出力音声が変わる」ことの
    プラットフォーム非依存な機械検証（Fish のキーが無くても回る）。
    macOS の日本語男性声(Otoya)は現代の macOS では既定で入っていないことがあるため、
    voice_catalog._SAY_GENDER_HINTS が male 扱いする候補から見つかった最初の1つを使う。
    """
    _librosa_or_skip()
    if not _has_say_voice("Kyoko"):
        pytest.skip("`say` に Kyoko(女性)が入っていない環境のためスキップ")
    male = _first_available_male_say()
    if male is None:
        pytest.skip("`say` に男性声(Otoya/Eddy/Reed/Rocko/Grandpa)がひとつも無い")

    cfg = _cfg()
    out_a = tmp_path / "female.wav"
    out_b = tmp_path / "male.wav"

    meta_a = tts.SayTTSBackend(voice="Kyoko").synthesize(_TEXT_JP, str(out_a), cfg)
    meta_b = tts.SayTTSBackend(voice=male).synthesize(_TEXT_JP, str(out_b), cfg)

    # サイレントフォールバックへ落ちていない（＝実音声が生成された）ことを確認
    if meta_a.get("is_silent") or meta_b.get("is_silent"):
        pytest.skip("say がサイレントfallbackに落ちた（音声デバイス不在の可能性）")

    f0_a = _f0_median_hz(str(out_a))
    f0_b = _f0_median_hz(str(out_b))
    assert f0_a is not None and f0_b is not None, (
        "f0推定不能（音声が空/短すぎ）: kyoko={} {}={}".format(f0_a, male, f0_b)
    )

    # 女性(Kyoko)は男性より明確に高い（差 >= 30Hz を最低ラインとする）。
    # 実測(2026-07-26 macOS): Kyoko ~265Hz / Eddy ~102Hz（差 >100Hz）。
    assert abs(f0_a - f0_b) >= 30.0, (
        "同一文で2声のf0差が小さすぎる: kyoko={:.1f}Hz {}={:.1f}Hz "
        "（=声が実質変わっていない疑い）".format(f0_a, male, f0_b)
    )
    # 想定される高低関係も検証（女性 > 男性）。
    assert f0_a > f0_b, (
        "Kyoko(女性)の方が高いはずが逆転: kyoko={:.1f} {}={:.1f}".format(f0_a, male, f0_b)
    )


@pytest.mark.skipif(
    not os.environ.get("FISH_AUDIO_API_KEY"),
    reason="FISH_AUDIO_API_KEY 未設定（実APIコスト回避のためデフォルトはskip）",
)
def test_paid_tier_two_fish_voices_f0_differ(tmp_path):
    """Fish Audio の女性声と男性声で実合成し、f0中央値が明確に異なることを実APIで検証する。

    実APIを2回叩く（低額課金/無料枠）。CI では FISH_AUDIO_API_KEY を渡さないためスキップされる。
    使う声IDは config.json 収録の実在確認済みID（2026-07-26 GET /model で存在確認）。
    """
    _librosa_or_skip()
    cfg = _cfg()
    female_id = "0089dce5fefb4c6ba9b9f2f0debe1ddc"  # 落ち着いた女性
    male_id = "45c5d3723c9c42f598e4776dcfd5f02d"  # 落ち着いた男性

    out_a = tmp_path / "fish_female.wav"
    out_b = tmp_path / "fish_male.wav"

    meta_a = tts.FishAudioTTSBackend(reference_id=female_id).synthesize(
        _TEXT_JP, str(out_a), cfg,
    )
    meta_b = tts.FishAudioTTSBackend(reference_id=male_id).synthesize(
        _TEXT_JP, str(out_b), cfg,
    )
    # Fish が失敗して say フォールバックへ落ちたら意味が無いのでスキップ扱いにする
    if meta_a.get("backend") != "fish_audio" or meta_b.get("backend") != "fish_audio":
        pytest.skip("Fish Audio がフォールバック（APIキー/ネットワーク不備）")

    f0_a = _f0_median_hz(str(out_a))
    f0_b = _f0_median_hz(str(out_b))
    assert f0_a is not None and f0_b is not None
    assert abs(f0_a - f0_b) >= 50.0, (
        "Fish 2声のf0差が小さすぎる: female={:.1f}Hz male={:.1f}Hz".format(f0_a, f0_b)
    )
    assert f0_a > f0_b, "女性IDの方が高いはずが逆転: {:.1f} vs {:.1f}".format(f0_a, f0_b)
