# -*- coding: utf-8 -*-
"""pipeline/voice_catalog.py: 声カタログ生成・自動選択・tier制限・手動指定・fallback の純関数テスト。

`say -v ?` は _say_run 差し替えで機械独立にする（実機依存はしない）。
"""
from pipeline import voice_catalog as vc


class _FakeProc:
    def __init__(self, text):
        self.stdout = text.encode("utf-8")


# 実機 `say -v ?` を模した固定出力（Kyoko=female / Otoya=male / Eddy=male novelty / Sandy=female）。
_FAKE_SAY = (
    "Kyoko               ja_JP    # こんにちは! 私の名前はKyokoです。\n"
    "Otoya               ja_JP    # こんにちは! 私の名前はOtoyaです。\n"
    "Eddy (日本語（日本）)      ja_JP    # こんにちは! 私の名前はEddyです。\n"
    "Sandy (日本語（日本）)     ja_JP    # こんにちは! 私の名前はSandyです。\n"
    "Samantha            en_US    # Hi, my name is Samantha.\n"
)


def _fake_run(text=_FAKE_SAY):
    return lambda cmd: _FakeProc(text)


def _cfg(engine="say", voice_mode="auto", fish=None):
    return {
        "voice": "Kyoko",
        "tts": {"engine": engine, "voice_mode": voice_mode},
        "voices": {"fish": fish or []},
    }


# --- 列挙 ---

def test_list_say_only_japanese():
    voices = vc.list_say_japanese_voices(_run=_fake_run())
    names = [v["name"] for v in voices]
    assert "Kyoko" in names and "Otoya" in names and "Eddy" in names
    assert "Samantha" not in names  # en_US は除外


def test_list_say_empty_when_command_fails():
    def _boom(cmd):
        raise OSError("no say")
    assert vc.list_say_japanese_voices(_run=_boom) == []


# --- カタログ ---

def test_build_catalog_free_and_paid():
    fish = [{"id": "abc123def456", "label": "男性ナレ", "gender": "male", "pitch": "low"}]
    cat = vc.build_catalog(_cfg(fish=fish), _say_run=_fake_run())
    keys = {e["key"] for e in cat}
    assert "say_kyoko" in keys and "fish_abc123de" in keys
    kyoko = next(e for e in cat if e["key"] == "say_kyoko")
    assert kyoko["tier"] == "free" and kyoko["engine"] == "say" and kyoko["gender"] == "female"
    fishe = next(e for e in cat if e["key"] == "fish_abc123de")
    assert fishe["tier"] == "paid" and fishe["engine"] == "fish"
    assert fishe["engine_voice_id"] == "abc123def456"


def test_build_catalog_skips_fish_without_id():
    cat = vc.build_catalog(_cfg(fish=[{"label": "no id"}]), _say_run=_fake_run())
    assert all(e["tier"] != "paid" for e in cat)


# --- 自動選択 ---

def test_auto_female_picks_female_classic():
    cfg = _cfg(engine="say")
    r = vc.resolve_voice(cfg, {"gender_guess": "female", "pitch": "mid"}, _say_run=_fake_run())
    assert r["gender"] == "female" and r["engine"] == "say" and r["mode"] == "auto"
    assert r["engine_voice_id"] == "Kyoko"  # 定番優先


def test_auto_male_picks_male():
    cfg = _cfg(engine="say")
    r = vc.resolve_voice(cfg, {"gender_guess": "male", "pitch": "low"}, _say_run=_fake_run())
    assert r["gender"] == "male" and r["engine"] == "say"
    assert r["engine_voice_id"] == "Otoya"  # 定番の男性を優先


def test_auto_unknown_falls_back_to_tier_default():
    # gender_guess=unknown → auto は tier 既定（cfg.voice=Kyoko）へ解決する
    cfg = _cfg(engine="say")
    r = vc.resolve_voice(cfg, {"gender_guess": "unknown"}, _say_run=_fake_run())
    assert r["engine_voice_id"] == "Kyoko"  # free 既定 = cfg.voice


def test_auto_no_narrator_voice_falls_back():
    cfg = _cfg(engine="say")
    r = vc.resolve_voice(cfg, None, _say_run=_fake_run())
    assert r["engine_voice_id"] == "Kyoko"


# --- tier 制限（0円保証: free は fish を選ばない） ---

def test_free_tier_never_selects_fish_even_manual():
    fish = [{"id": "abc123def456", "gender": "female"}]
    cfg = _cfg(engine="say", voice_mode="fish_abc123de", fish=fish)
    r = vc.resolve_voice(cfg, None, _say_run=_fake_run())
    assert r["engine"] == "say"  # fish 指定でも free では say へ
    assert r["mode"] == "fallback"


def test_free_tier_auto_ignores_fish_for_female():
    fish = [{"id": "abc123def456", "gender": "female"}]
    cfg = _cfg(engine="say", fish=fish)
    r = vc.resolve_voice(cfg, {"gender_guess": "female", "pitch": "mid"}, _say_run=_fake_run())
    assert r["engine"] == "say" and r["tier"] == "free"


def test_paid_tier_auto_can_select_fish():
    fish = [{"id": "abc123def456", "label": "女性", "gender": "female", "pitch": "mid"}]
    cfg = _cfg(engine="fish_audio", fish=fish)
    r = vc.resolve_voice(cfg, {"gender_guess": "female", "pitch": "mid"}, _say_run=_fake_run())
    assert r["engine"] == "fish" and r["engine_voice_id"] == "abc123def456"


# --- 手動指定 ---

def test_manual_key_selects_that_voice():
    cfg = _cfg(engine="say", voice_mode="say_otoya")
    r = vc.resolve_voice(cfg, None, _say_run=_fake_run())
    assert r["key"] == "say_otoya" and r["engine_voice_id"] == "Otoya" and r["mode"] == "manual"


def test_manual_unknown_key_falls_back():
    cfg = _cfg(engine="say", voice_mode="say_doesnotexist")
    r = vc.resolve_voice(cfg, None, _say_run=_fake_run())
    assert r["mode"] == "fallback" and r["engine_voice_id"] == "Kyoko"


def test_resolve_never_returns_none_even_without_voices():
    # say が空 + fish 空 の極端環境でも合成 Kyoko を返す
    cfg = _cfg(engine="say")
    r = vc.resolve_voice(cfg, None, _say_run=lambda: (_ for _ in ()).throw(OSError()))
    # _say_run に渡すのは runner。ここでは失敗 runner を渡す。
    def _boom(cmd):
        raise OSError()
    r = vc.resolve_voice(cfg, None, _say_run=_boom)
    assert r["engine"] == "say" and r["engine_voice_id"] == "Kyoko"
