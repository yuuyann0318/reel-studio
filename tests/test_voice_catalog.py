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


# --- 複数 Fish 声での auto 選択（声の調達後: 参考の gender/pitch に近い声を選べる） ---

# config.voices.fish を模した複数声（実試聴で gender/pitch を確定した想定の登録データ）。
_MULTI_FISH = [
    {"id": "f_bright_female", "label": "元気な女性", "gender": "female", "pitch": "mid", "f0_hz": 231.4},
    {"id": "f_calm_female", "label": "落ち着いた女性", "gender": "female", "pitch": "high", "f0_hz": 284.9},
    {"id": "m_calm_male", "label": "落ち着いた男性", "gender": "male", "pitch": "low", "f0_hz": 140.0},
    {"id": "m_biz_male", "label": "ビジネス男性", "gender": "male", "pitch": "mid", "f0_hz": 149.2},
]


def test_f0_hz_threaded_into_catalog():
    cat = vc.build_catalog(_cfg(engine="fish_audio", fish=_MULTI_FISH), _say_run=_fake_run())
    fe = next(e for e in cat if e.get("engine_voice_id") == "f_bright_female")
    assert fe["f0_hz"] == 231.4 and fe["gender"] == "female" and fe["tier"] == "paid"


def test_paid_auto_female_high_picks_high_female():
    # 参考が female/high → 女性の中で pitch=high の声を選ぶ（複数女性から参考準拠）
    cfg = _cfg(engine="fish_audio", fish=_MULTI_FISH)
    r = vc.resolve_voice(cfg, {"gender_guess": "female", "pitch": "high"}, _say_run=_fake_run())
    assert r["engine"] == "fish" and r["gender"] == "female"
    assert r["engine_voice_id"] == "f_calm_female"  # high の女性


def test_paid_auto_female_mid_picks_mid_female():
    # 参考が female/mid → 女性の中で pitch=mid の声（元気な女性）を選ぶ
    cfg = _cfg(engine="fish_audio", fish=_MULTI_FISH)
    r = vc.resolve_voice(cfg, {"gender_guess": "female", "pitch": "mid"}, _say_run=_fake_run())
    assert r["gender"] == "female" and r["engine_voice_id"] == "f_bright_female"


def test_paid_auto_male_low_picks_low_male():
    # 参考が male/low → 男性の中で pitch=low の声を選ぶ（女性は候補から除外される）
    cfg = _cfg(engine="fish_audio", fish=_MULTI_FISH)
    r = vc.resolve_voice(cfg, {"gender_guess": "male", "pitch": "low"}, _say_run=_fake_run())
    assert r["gender"] == "male" and r["engine_voice_id"] == "m_calm_male"


def test_paid_auto_male_never_picks_female():
    # gender 一致が最優先: male 参考は女性声を絶対に選ばない
    cfg = _cfg(engine="fish_audio", fish=_MULTI_FISH)
    r = vc.resolve_voice(cfg, {"gender_guess": "male", "pitch": "mid"}, _say_run=_fake_run())
    assert r["gender"] == "male"


# --- Fish 公式API 動的取得（声の調達） ---

# GET /model の応答を模したフィクスチャ（実APIのフィールドに準拠: _id/title/type/languages/task_count）。
def _fake_model_response():
    return {
        "total": 3,
        "items": [
            {"_id": "id_pop", "title": "元気な女性", "type": "tts",
             "languages": ["ja"], "task_count": 336091, "like_count": 1237},
            {"_id": "id_svc", "title": "歌声変換モデル", "type": "svc",
             "languages": ["ja"], "task_count": 999, "like_count": 1},
            {"_id": "id_second", "title": "落ち着いた男性", "type": "tts",
             "languages": ["ja"], "task_count": 51614, "like_count": 483},
        ],
        "has_more": True,
    }


def test_fetch_fish_voices_parses_and_filters_svc():
    captured = {}

    def _get(url, api_key, timeout_sec):
        captured["url"] = url
        captured["key"] = api_key
        return _fake_model_response()

    voices = vc.fetch_fish_voices(api_key="k", language="ja", limit=8, _http_get=_get)
    ids = [v["id"] for v in voices]
    assert ids == ["id_pop", "id_second"]  # svc は除外・順序保持
    assert voices[0]["title"] == "元気な女性" and voices[0]["task_count"] == 336091
    assert "language=ja" in captured["url"] and "sort_by=task_count" in captured["url"]
    assert captured["key"] == "k"


def test_fetch_fish_voices_limit_respected():
    def _get(url, api_key, timeout_sec):
        return _fake_model_response()
    voices = vc.fetch_fish_voices(api_key="k", limit=1, _http_get=_get)
    assert len(voices) == 1 and voices[0]["id"] == "id_pop"
    # page_size がクエリに反映される
    # （limit=1 のとき page_size=1）
    def _get2(url, api_key, timeout_sec):
        assert "page_size=1" in url
        return _fake_model_response()
    vc.fetch_fish_voices(api_key="k", limit=1, _http_get=_get2)


def test_fetch_fish_voices_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    # spy: 呼ばれた回数を外側で数える（fetch 側が Exception を握り潰しても検出できる）
    calls = {"n": 0}

    def _get(url, api_key, timeout_sec):
        calls["n"] += 1
        return {"items": []}

    assert vc.fetch_fish_voices(api_key=None, _http_get=_get) == []
    assert calls["n"] == 0, "キー無しでHTTPが呼ばれた（呼び出し前ガードが機能していない）"


def test_fetch_fish_voices_uses_env_key(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "envkey")
    seen = {}

    def _get(url, api_key, timeout_sec):
        seen["key"] = api_key
        return {"items": [{"_id": "x", "title": "t", "type": "tts"}]}

    voices = vc.fetch_fish_voices(api_key=None, _http_get=_get)
    assert seen["key"] == "envkey" and voices[0]["id"] == "x"


def test_fetch_fish_voices_http_error_returns_empty():
    def _get(url, api_key, timeout_sec):
        raise OSError("network down")
    assert vc.fetch_fish_voices(api_key="k", _http_get=_get) == []


def test_fetch_fish_voices_malformed_returns_empty():
    def _get(url, api_key, timeout_sec):
        return {"unexpected": True}
    assert vc.fetch_fish_voices(api_key="k", _http_get=_get) == []


# --- キャッシュ ---

def test_cache_roundtrip_and_ttl(tmp_path):
    p = str(tmp_path / "fish_cache.json")

    def _get(url, api_key, timeout_sec):
        return _fake_model_response()

    saved = vc.refresh_fish_catalog_cache(p, api_key="k", limit=8, _http_get=_get,
                                          _now=lambda: 1000.0)
    assert [v["id"] for v in saved] == ["id_pop", "id_second"]

    # 期限内（max_age 大）→ 読める
    loaded = vc.load_cached_fish_voices(p, max_age_sec=100.0, _now=lambda: 1050.0)
    assert [v["id"] for v in loaded] == ["id_pop", "id_second"]

    # 期限切れ（60秒制限・150秒経過）→ 空
    assert vc.load_cached_fish_voices(p, max_age_sec=60.0, _now=lambda: 1150.0) == []


def test_cache_not_written_on_empty_fetch(tmp_path):
    p = str(tmp_path / "nope.json")

    def _get(url, api_key, timeout_sec):
        raise OSError("down")

    assert vc.refresh_fish_catalog_cache(p, api_key="k", _http_get=_get) == []
    import os
    assert not os.path.exists(p)  # 取得0件では古いキャッシュを壊さない


def test_load_cache_missing_file_returns_empty(tmp_path):
    assert vc.load_cached_fish_voices(str(tmp_path / "absent.json")) == []


# --- svc 混入時に limit まで tts で埋めるためのページング ---

def test_fetch_paginates_when_svc_pollutes_first_page():
    """1ページ目が svc だらけで tts が limit に満たない場合、次ページから tts を補充する。

    実 API 応答は tts + svc 混在であり、page_size 分の tts を保証しない。
    従来（単発GET＋末尾で svc filter）だと欠落するリグレッションを再現するテスト。
    """
    calls = {"n": 0, "urls": []}

    def _get(url, api_key, timeout_sec):
        calls["n"] += 1
        calls["urls"].append(url)
        if "page_number=1" in url:
            return {
                "items": [
                    {"_id": "svc1", "title": "svc a", "type": "svc"},
                    {"_id": "svc2", "title": "svc b", "type": "svc"},
                    {"_id": "tts1", "title": "tts a", "type": "tts"},
                ],
                "has_more": True,
            }
        if "page_number=2" in url:
            return {
                "items": [
                    {"_id": "tts2", "title": "tts b", "type": "tts"},
                    {"_id": "tts3", "title": "tts c", "type": "tts"},
                ],
                "has_more": False,
            }
        raise AssertionError("想定外のページ: " + url)

    voices = vc.fetch_fish_voices(api_key="k", limit=3, _http_get=_get)
    assert [v["id"] for v in voices] == ["tts1", "tts2", "tts3"]
    assert calls["n"] == 2  # 2ページ読んで確実に埋めた


def test_fetch_stops_on_has_more_false():
    """has_more=False なら limit 未達でもそこで打ち切る（無限ループしない）。"""
    def _get(url, api_key, timeout_sec):
        return {
            "items": [{"_id": "only1", "title": "x", "type": "tts"}],
            "has_more": False,
        }
    voices = vc.fetch_fish_voices(api_key="k", limit=10, _http_get=_get, max_pages=99)
    assert [v["id"] for v in voices] == ["only1"]


def test_fetch_respects_max_pages_safety_cap():
    """max_pages に達したら止まる（サーバが has_more を返さなくても暴走しない）。"""
    calls = {"n": 0}

    def _get(url, api_key, timeout_sec):
        calls["n"] += 1
        # 常に svc のみ・has_more キー無し → 打ち切り条件は max_pages のみ
        return {"items": [{"_id": "s{}".format(calls["n"]), "type": "svc"}]}

    vc.fetch_fish_voices(api_key="k", limit=10, _http_get=_get, max_pages=3)
    assert calls["n"] == 3


# --- 参考 f0[Hz] を使った同一 pitch ラベル内の近接選択（Fish 複数声のタイブレーク） ---

# female かつ pitch="high" が3声（f0 265/285/307Hz）。「参考に近い声」を pitch ラベル内で
# さらに f0 距離で選び分けられることを検証する。
_HIGH_FEMALE_FISH = [
    {"id": "f_low_high", "gender": "female", "pitch": "high", "f0_hz": 265.0},
    {"id": "f_mid_high", "gender": "female", "pitch": "high", "f0_hz": 285.0},
    {"id": "f_top_high", "gender": "female", "pitch": "high", "f0_hz": 307.0},
]


def test_f0_tiebreak_picks_closest_when_reference_provides_hz():
    cfg = _cfg(engine="fish_audio", fish=_HIGH_FEMALE_FISH)
    # 参考 f0 = 300Hz → 307Hz(最も近い) を選ぶ
    r = vc.resolve_voice(
        cfg,
        {"gender_guess": "female", "pitch": "high", "f0_median_hz": 300.0},
        _say_run=_fake_run(),
    )
    assert r["engine_voice_id"] == "f_top_high"

    # 参考 f0 = 268Hz → 265Hz(最も近い) を選ぶ
    r2 = vc.resolve_voice(
        cfg,
        {"gender_guess": "female", "pitch": "high", "f0_median_hz": 268.0},
        _say_run=_fake_run(),
    )
    assert r2["engine_voice_id"] == "f_low_high"


def test_f0_tiebreak_falls_back_to_order_when_no_hz_hint():
    # 参考に f0_median_hz が無い場合は従来どおり登場順（=先頭）で決定論的に選ぶ
    cfg = _cfg(engine="fish_audio", fish=_HIGH_FEMALE_FISH)
    r = vc.resolve_voice(
        cfg,
        {"gender_guess": "female", "pitch": "high"},
        _say_run=_fake_run(),
    )
    assert r["engine_voice_id"] == "f_low_high"  # 先頭


# --- 実在ID の整合性（オプトイン: FISH_AUDIO_API_KEY が有るときだけ実APIで検証） ---

import os as _os
import pytest as _pytest


@_pytest.mark.skipif(
    not _os.environ.get("FISH_AUDIO_API_KEY"),
    reason="FISH_AUDIO_API_KEY 未設定（実在ID検証は opt-in）",
)
def test_registered_fish_ids_are_real_on_upstream():
    """config.json の voices.fish に登録された全 ID が、Fish Audio 公式 API 側で実在することを検証する。

    実 API を叩く（title 部分一致検索でその ID を狙い撃ち→total>=1 を確認）。
    CI ではキー未設定でスキップ。ID の失効/誤記を検出する唯一の一次ソース確認。
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur
    from pathlib import Path as _P

    cfg_path = _P(__file__).resolve().parent.parent / "config.json"
    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
    ids = [v["id"] for v in (cfg.get("voices") or {}).get("fish") or []]
    assert ids, "config.voices.fish が空（登録が消えた?）"

    key = _os.environ["FISH_AUDIO_API_KEY"]

    def _exists(vid):
        # /model/{id} が公式にあるかは環境依存なので、確実な list 側で title 検索は不安定。
        # ここでは list を人気順で 100 件ほど広めに取って ID を照合するだけの軽い確認にする。
        # コスト: GET 1回/呼び出し。
        params = _up.urlencode({"language": "ja", "sort_by": "task_count",
                                "page_size": 100, "page_number": 1})
        req = _ur.Request("https://api.fish.audio/model?" + params)
        req.add_header("Authorization", "Bearer " + key)
        data = _json.loads(_ur.urlopen(req, timeout=30).read())
        found_ids = {(it.get("_id") or it.get("id")) for it in (data.get("items") or [])}
        return vid in found_ids

    # 1回だけ広めに取って全 ID を照合（実 API 節約）。人気順 top100 に無ければ個別確認する。
    params = _up.urlencode({"language": "ja", "sort_by": "task_count",
                            "page_size": 100, "page_number": 1})
    req = _ur.Request("https://api.fish.audio/model?" + params)
    req.add_header("Authorization", "Bearer " + key)
    top100 = _json.loads(_ur.urlopen(req, timeout=30).read())
    found = {(it.get("_id") or it.get("id")) for it in (top100.get("items") or [])}

    missing = [vid for vid in ids if vid not in found]
    # top100 に無いだけの可能性もあるので、fetch_fish_voices 経由でさらに広く見る
    if missing:
        deeper = vc.fetch_fish_voices(api_key=key, language="ja", limit=200, max_pages=5)
        deep_ids = {v["id"] for v in deeper}
        missing = [vid for vid in missing if vid not in deep_ids]

    assert not missing, "config.voices.fish の以下IDが Fish Audio 側に見つからない: {}".format(missing)
