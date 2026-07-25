# -*- coding: utf-8 -*-
"""外部実物SFX統合スクリプトと編集レシピ複数化のテスト。

対象:
    - assets/sfx/integrate_external.py（family推定・long除外・upsert）
    - pipeline.edit_profile.pick_cut_sfx の外部優先重み
    - pipeline.edit_profile.load_edit_profile / pick_edit_recipe の
      seed+mood 決定論選択、後方互換フォールバック、density フィルタ
    - studio/server/jobs.py 経由の project["edit_recipe"] 記録

実 ffmpeg は使わない（ffprobe を叩く箇所は monkeypatch）。
Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from pipeline import edit_profile
from pipeline import render


_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATE_SCRIPT = _REPO_ROOT / "assets" / "sfx" / "integrate_external.py"
_RECIPES_PATH = _REPO_ROOT / "assets" / "profiles" / "edit_recipes.json"


def _load_integrate_module():
    spec = importlib.util.spec_from_file_location("integrate_external", _INTEGRATE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# integrate_external.py: family推定 / long除外 / upsert
# ---------------------------------------------------------------------------

def test_integrate_classify_family_matches_keyword_map():
    """ファイル名キーワードから family を推定する（accent/whoosh/shimmer/impact/riser/texture）。"""
    ie = _load_integrate_module()
    assert ie.classify_family("button_decision1.mp3") == "accent"
    assert ie.classify_family("button_cursor5.mp3") == "accent"
    assert ie.classify_family("battle_swing.mp3") == "whoosh"
    assert ie.classify_family("anime_air-horn1.mp3") == "whoosh"
    assert ie.classify_family("anime_kira1.mp3") == "shimmer"
    assert ie.classify_family("anime_bell1.mp3") == "shimmer"
    assert ie.classify_family("battle_blow1.mp3") == "impact"
    assert ie.classify_family("anime_ban1.mp3") == "impact"
    assert ie.classify_family("machine_booster1.mp3") == "riser"
    assert ie.classify_family("anime_anxiety-piano1.mp3") == "riser"
    # マッチしないものは texture へ
    assert ie.classify_family("environment_bath1.mp3") == "texture"
    assert ie.classify_family("environment_airport1.mp3") == "texture"


def test_integrate_build_tags_flags_long_over_threshold():
    """尺 > 2.5秒 なら "long" タグを付与し、以下ならつけない。"""
    ie = _load_integrate_module()
    short_tags = ie.build_tags("impact", duration_sec=0.4)
    long_tags = ie.build_tags("impact", duration_sec=3.5)
    assert "long" not in short_tags
    assert "long" in long_tags
    # family に応じた意味タグと "external" が常に入る
    assert "impact" in short_tags
    assert "external" in short_tags


def test_integrate_upsert_preserves_existing_and_adds_new(tmp_path):
    """既存 manifest の legacy/合成SFXエントリを保全しつつ、新規 external を追加できる。"""
    ie = _load_integrate_module()
    existing = [
        {"file": "whoosh_01.wav", "label_jp": "既存", "family": "legacy", "tags": ["transition"],
         "duration": 0.5, "license": "synthetic-placeholder ..."},
        {"file": "hand_manual.wav", "label_jp": "手動", "family": "custom", "tags": ["manual"],
         "duration": 0.3, "license": "manual"},
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    new_entries = [
        {"file": "external/foo.mp3", "label_jp": "ext:foo", "family": "impact",
         "tags": ["impact", "external"], "duration": 0.7, "source": "external",
         "license": "external (...)"}
    ]
    result = ie.upsert_manifest(new_entries, manifest_path=manifest_path)
    files = [e["file"] for e in result]
    assert "whoosh_01.wav" in files  # 保全
    assert "hand_manual.wav" in files  # 手動追記も保全
    assert "external/foo.mp3" in files  # 新規追加
    # ディスクからも読み直して同じ
    reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [e["file"] for e in reloaded] == files


def test_integrate_upsert_overwrites_when_same_file_key(tmp_path):
    """同じ file 名を再統合すると既存エントリが新版で置き換わる（順序保持）。"""
    ie = _load_integrate_module()
    existing = [
        {"file": "external/foo.mp3", "label_jp": "old", "family": "texture",
         "tags": ["ambient", "external"], "duration": 5.0, "source": "external",
         "license": "external"},
        {"file": "whoosh_01.wav", "family": "legacy", "tags": ["transition"],
         "duration": 0.5, "license": "synthetic"},
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    new_entries = [
        {"file": "external/foo.mp3", "label_jp": "new", "family": "impact",
         "tags": ["impact", "external"], "duration": 0.6, "source": "external",
         "license": "external"}
    ]
    result = ie.upsert_manifest(new_entries, manifest_path=manifest_path)
    # 順序: 既存の並びを維持したまま file 内容だけ差し替わる
    assert result[0]["file"] == "external/foo.mp3"
    assert result[0]["family"] == "impact"
    assert result[0]["label_jp"] == "new"
    assert result[1]["file"] == "whoosh_01.wav"  # legacy はそのまま


def test_integrate_scan_uses_ffprobe_for_duration(tmp_path, monkeypatch):
    """scan_external は ffprobe で尺を取り、>2.5秒の音源に long タグを付ける。"""
    ie = _load_integrate_module()
    ext = tmp_path / "external"
    ext.mkdir()
    # 4 個のファイルを配置 (実 mp3 は不要 = 存在確認のみで scan は動く)
    for name in ["anime_kira1.mp3", "button_decision1.mp3", "environment_bath1.mp3",
                 "battle_blow1.mp3"]:
        (ext / name).write_bytes(b"placeholder")

    # ffprobe を「ファイル名で尺を返す」モックへ差し替え。
    def fake_probe(path, ffprobe_bin=None):
        n = Path(path).name
        if "bath" in n:
            return 4.0  # >2.5s -> long
        return 0.5
    monkeypatch.setattr(ie, "probe_duration", fake_probe)

    entries = ie.scan_external(external_dir=ext, ffprobe_bin="/dev/null")
    by_name = {e["file"]: e for e in entries}
    assert by_name["external/anime_kira1.mp3"]["family"] == "shimmer"
    assert by_name["external/button_decision1.mp3"]["family"] == "accent"
    assert by_name["external/environment_bath1.mp3"]["family"] == "texture"
    assert by_name["external/battle_blow1.mp3"]["family"] == "impact"
    # 4.0s は long
    assert "long" in by_name["external/environment_bath1.mp3"]["tags"]
    assert "long" not in by_name["external/anime_kira1.mp3"]["tags"]
    # source は "external"
    assert all(e["source"] == "external" for e in entries)


# ---------------------------------------------------------------------------
# pick_cut_sfx: 外部優先(80/20)重み + long タグ除外
# ---------------------------------------------------------------------------

def _mixed_manifest():
    """同じ family(impact) に external 4件 + gen 1件 を配置した manifest。"""
    return [
        {"file": "external/impact_ext_a.mp3", "family": "impact", "tags": ["impact", "external"], "source": "external"},
        {"file": "external/impact_ext_b.mp3", "family": "impact", "tags": ["impact", "external"], "source": "external"},
        {"file": "external/impact_ext_c.mp3", "family": "impact", "tags": ["impact", "external"], "source": "external"},
        {"file": "external/impact_ext_d.mp3", "family": "impact", "tags": ["impact", "external"], "source": "external"},
        {"file": "impact_gen_001.wav", "family": "impact", "tags": ["impact"], "source": None},
    ]


def test_pick_cut_sfx_prefers_external_over_gen_when_both_available():
    """external と gen の混在時、多数試行では external の採用比が明確に多い（4:1 重み）。"""
    m = _mixed_manifest()
    weights = {"impact": 10}
    external_hits = 0
    total = 100
    for i in range(total):
        picked = edit_profile.pick_cut_sfx("mix-seed-{}".format(i), 0, m, family_weights=weights)
        if picked and picked.get("source") == "external":
            external_hits += 1
    # 期待 80% だが、決定論擬似乱数のばらつきを考慮し 60% 以上で合格
    assert external_hits > total * 0.6, (
        "expected external majority, got {}/{}".format(external_hits, total)
    )


def test_pick_cut_sfx_uses_gen_when_no_external_in_family():
    """external が無い family では従来どおり合成SFXから選ばれる（後方互換）。"""
    m = [
        {"file": "impact_gen_001.wav", "family": "impact", "tags": ["impact"], "source": None},
        {"file": "impact_gen_002.wav", "family": "impact", "tags": ["impact"], "source": None},
    ]
    picked = edit_profile.pick_cut_sfx("s", 0, m, family_weights={"impact": 5})
    assert picked is not None
    assert picked["source"] is None


def test_pick_cut_sfx_excludes_long_tagged_entries():
    """long タグ (>2.5秒) のエントリはカットSE候補から除外される。"""
    m = [
        {"file": "long_bath.mp3", "family": "impact", "tags": ["impact", "external", "long"], "source": "external"},
        {"file": "short_blow.mp3", "family": "impact", "tags": ["impact", "external"], "source": "external"},
    ]
    # 何度回しても long は選ばれない
    for i in range(30):
        picked = edit_profile.pick_cut_sfx("s{}".format(i), i, m, family_weights={"impact": 5})
        assert picked is not None
        assert "long" not in picked.get("tags", [])
        assert picked["file"] == "short_blow.mp3"


def test_pick_cut_sfx_excludes_texture_family_entries():
    """family=='texture' のエントリはカットSE候補から除外される（環境音/演出音は
    単発カットSEには向かないため）。"""
    m = [
        # texture: 短音でも除外される（LONG_TAG 有無に関わらず）
        {"file": "external/env_bath.mp3", "family": "texture",
         "tags": ["ambient", "external"], "source": "external"},
        {"file": "external/env_airport.mp3", "family": "texture",
         "tags": ["ambient", "external", "long"], "source": "external"},
        # impact: 選ばれる正解
        {"file": "external/blow.mp3", "family": "impact",
         "tags": ["impact", "external"], "source": "external"},
    ]
    for i in range(20):
        picked = edit_profile.pick_cut_sfx(
            "seed-{}".format(i), i, m,
            family_weights={"impact": 5, "texture": 5},  # texture の重みがあっても除外される
        )
        assert picked is not None
        assert picked.get("family") != "texture", picked
        assert picked["file"] == "external/blow.mp3"


def test_pick_cut_sfx_returns_none_when_only_texture_candidates_available():
    """候補が texture のみ（長尺含む）なら候補全滅で None を返す。"""
    m = [
        {"file": "external/env_bath.mp3", "family": "texture",
         "tags": ["ambient", "external"], "source": "external"},
        {"file": "external/env_long.mp3", "family": "texture",
         "tags": ["ambient", "external", "long"], "source": "external"},
    ]
    picked = edit_profile.pick_cut_sfx("s", 0, m, family_weights={"texture": 5})
    assert picked is None


# ---------------------------------------------------------------------------
# integrate_external.py: texture かつ >2.5秒 は必ず LONG_TAG が付く（不変式）
# ---------------------------------------------------------------------------

def test_integrate_texture_over_threshold_always_gets_long_tag(tmp_path, monkeypatch):
    """family=='texture' かつ duration>2.5秒 のエントリには LONG_TAG が必ず付く。

    render 側の _filter_cut_sfx_candidates は texture を除外するが、環境音の
    誤分類（family 未推定＝texture）が短くて長尺タグも無いままカットSE候補に
    残ってしまう回帰を防ぐため、上流(integrate_external.py)で texture 長尺 →
    LONG_TAG の一意な組み合わせを固定する。
    """
    ie = _load_integrate_module()
    ext = tmp_path / "external"
    ext.mkdir()
    # 分類ルールに引っかからない名前 = texture 扱い
    long_texture = ext / "environment_bath_long1.mp3"
    long_texture.write_bytes(b"placeholder")
    short_texture = ext / "environment_airport_short1.mp3"
    short_texture.write_bytes(b"placeholder")

    def fake_probe(path, ffprobe_bin=None):
        return 5.0 if "long" in Path(path).name else 0.5
    monkeypatch.setattr(ie, "probe_duration", fake_probe)
    monkeypatch.setattr(ie, "probe_mean_volume", lambda *a, **kw: None)

    entries = ie.scan_external(external_dir=ext)
    by_name = {e["file"]: e for e in entries}
    long_entry = by_name["external/environment_bath_long1.mp3"]
    short_entry = by_name["external/environment_airport_short1.mp3"]
    assert long_entry["family"] == "texture"
    assert short_entry["family"] == "texture"
    assert "long" in long_entry["tags"], (
        "texture かつ >2.5秒 なのに LONG_TAG が付いていない: {}".format(long_entry)
    )
    # 短い texture には long は付かない（既存挙動）
    assert "long" not in short_entry["tags"]


# ---------------------------------------------------------------------------
# integrate_external.py: mean_volume_db 計測とmanifest保存
# ---------------------------------------------------------------------------

def test_integrate_probe_mean_volume_parses_volumedetect_output(monkeypatch):
    """probe_mean_volume は ffmpeg volumedetect 出力から mean_volume(dB) を抜き出す。"""
    ie = _load_integrate_module()

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = (
            b"[Parsed_volumedetect_0 @ 0x600003ea4bb0] n_samples: 176400\n"
            b"[Parsed_volumedetect_0 @ 0x600003ea4bb0] mean_volume: -24.1 dB\n"
            b"[Parsed_volumedetect_0 @ 0x600003ea4bb0] max_volume: -8.4 dB\n"
        )

    monkeypatch.setattr(ie.subprocess, "run", lambda *a, **kw: _Proc())
    v = ie.probe_mean_volume("/dev/null", ffmpeg_bin="/dev/null")
    assert v == pytest.approx(-24.1)


def test_integrate_probe_mean_volume_returns_none_when_not_found(monkeypatch):
    """volumedetect の出力に mean_volume 行が無ければ None を返す（例外にしない）。"""
    ie = _load_integrate_module()

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"Invalid data\n"

    monkeypatch.setattr(ie.subprocess, "run", lambda *a, **kw: _Proc())
    assert ie.probe_mean_volume("/dev/null", ffmpeg_bin="/dev/null") is None


def test_integrate_scan_records_mean_volume_db(tmp_path, monkeypatch):
    """scan_external は各エントリに mean_volume_db を保存する（None も許容）。"""
    ie = _load_integrate_module()
    ext = tmp_path / "external"
    ext.mkdir()
    (ext / "battle_blow1.mp3").write_bytes(b"placeholder")
    (ext / "anime_kira1.mp3").write_bytes(b"placeholder")

    monkeypatch.setattr(ie, "probe_duration", lambda *a, **kw: 0.5)

    def fake_mean(path, ffmpeg_bin=None):
        return -8.0 if "blow" in Path(path).name else -22.5

    monkeypatch.setattr(ie, "probe_mean_volume", fake_mean)
    entries = ie.scan_external(external_dir=ext)
    by_name = {e["file"]: e for e in entries}
    assert by_name["external/battle_blow1.mp3"]["mean_volume_db"] == pytest.approx(-8.0)
    assert by_name["external/anime_kira1.mp3"]["mean_volume_db"] == pytest.approx(-22.5)


# ---------------------------------------------------------------------------
# render.py: SFX gain 補正 (normalize_sfx_gain_db, build_sfx_overlay_filters)
# ---------------------------------------------------------------------------

def test_normalize_sfx_gain_boosts_quiet_source_toward_target():
    """mean_volume=-24 (target -12 より小さい) は +12dB のブーストが乗る(BUG-53: target -14→-12)."""
    out = render.normalize_sfx_gain_db(-18.0, -24.0)
    assert out == pytest.approx(-18.0 + 12.0)


def test_normalize_sfx_gain_cuts_loud_source_toward_target():
    """mean_volume=-8 (target -12 より大きい) は -4dB のカットが乗る(BUG-53: target -14→-12)."""
    out = render.normalize_sfx_gain_db(-18.0, -8.0)
    assert out == pytest.approx(-18.0 - 4.0)


def test_normalize_sfx_gain_clamps_boost_to_plus_max():
    """target との差が MAX_ADJUST_DB を超える場合はクランプされる(無音源の過大ブースト回避)."""
    # mean_volume を極端に小さくして adjust の絶対値が MAX_ADJUST_DB(18) を超えるようにする
    over_by = render.SFX_MEAN_VOLUME_MAX_ADJUST_DB + 10.0
    mv = render.SFX_MEAN_VOLUME_TARGET_DB - over_by  # adjust = target-mv = +over_by
    out = render.normalize_sfx_gain_db(-18.0, mv)
    assert out == pytest.approx(-18.0 + render.SFX_MEAN_VOLUME_MAX_ADJUST_DB)


def test_normalize_sfx_gain_clamps_cut_to_minus_max():
    """target との差が -MAX_ADJUST_DB を超える場合はクランプされる(クリッピング一直線のカット回避)."""
    # mean_volume を強めに設定して adjust の絶対値が MAX_ADJUST_DB(18) を超えるようにする
    over_by = render.SFX_MEAN_VOLUME_MAX_ADJUST_DB + 5.0
    mv = render.SFX_MEAN_VOLUME_TARGET_DB + over_by  # adjust = target-mv = -over_by
    out = render.normalize_sfx_gain_db(-18.0, mv)
    assert out == pytest.approx(-18.0 - render.SFX_MEAN_VOLUME_MAX_ADJUST_DB)


def test_normalize_sfx_gain_uses_assumed_mean_when_missing_bug53():
    """BUG-53: mean_volume_db が None(合成SFX等)でも SFX_ASSUMED_MEAN_VOLUME_DB(-20) で補正する。
    従来は補正なしで base_gain のまま返していたため、合成SFX が最終ミックスで埋もれていた。
    target=-12 と assumed_mean=-20 の差 +8dB が base_gain に加算される。
    """
    out = render.normalize_sfx_gain_db(-18.0, None)
    expected_adjust = render.SFX_MEAN_VOLUME_TARGET_DB - render.SFX_ASSUMED_MEAN_VOLUME_DB
    assert out == pytest.approx(-18.0 + expected_adjust)


def test_build_sfx_overlay_filters_applies_mean_volume_normalization():
    """build_sfx_overlay_filters は spec['mean_volume_db'] を見て volume 係数を補正する。"""
    sfx = [
        {"path": "/a.mp3", "at_sec": 1.0, "gain_db": -18.0, "mean_volume_db": -24.0},
        {"path": "/b.mp3", "at_sec": 2.0, "gain_db": -18.0, "mean_volume_db": -8.0},
        {"path": "/c.mp3", "at_sec": 3.0, "gain_db": -18.0},  # mean 未指定 → assumed_mean 補正
    ]
    parts, labels = render.build_sfx_overlay_filters(sfx, start_index=10)
    # BUG-53(target -16→-12):
    # a: base -18 + adjust (=-12-(-24)=+12) = -6dB → linear = 10^(-6/20) ≒ 0.5012
    # b: base -18 + adjust (=-12-(-8) =-4)  = -22dB → linear = 10^(-22/20) ≒ 0.0794
    # c: base -18 + adjust (=-12-(-20)=+8) = -10dB → linear = 10^(-10/20) ≒ 0.3162
    assert "volume=0.5012" in parts[0]
    assert "volume=0.0794" in parts[1]
    assert "volume=0.3162" in parts[2]
    # ラベルと adelay は従来どおり
    assert "[sfx0]" in parts[0]
    assert "adelay=1000|1000" in parts[0]
    assert labels == ["[sfx0]", "[sfx1]", "[sfx2]"]


def test_build_edit_cut_sfx_specs_propagates_mean_volume_db(monkeypatch):
    """rotate モードで manifest 由来の mean_volume_db が spec に伝搬する。"""
    fake_manifest = [
        {"file": "external/blow.mp3", "family": "impact",
         "tags": ["impact", "external"], "source": "external",
         "mean_volume_db": -8.0},
    ]
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name) if name else None,
    )
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "density": "every",
            "family_weights": {"impact": 10},
            "manifest": fake_manifest,
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    specs = render.build_edit_cut_sfx_specs(
        [0.0, 2.0], profile, project_seed="seedMV",
        hook_end_sec=2.0, cta_start_sec=5.0,
    )
    assert len(specs) == 1
    assert specs[0]["mean_volume_db"] == pytest.approx(-8.0)


def test_load_sfx_manifest_propagates_mean_volume_db(tmp_path, monkeypatch):
    """load_sfx_manifest は entry の mean_volume_db を正規化して保持する。"""
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps([
        {"file": "external/x.mp3", "family": "impact", "tags": ["impact"],
         "source": "external", "duration": 0.5, "mean_volume_db": -12.3},
        {"file": "impact_gen_001.wav", "family": "impact", "tags": ["impact"],
         "duration": 0.5},  # 欠損 → None
    ]), encoding="utf-8")
    monkeypatch.setattr(edit_profile, "_SFX_MANIFEST_PATH", mpath)
    m = edit_profile.load_sfx_manifest()
    by_file = {e["file"]: e for e in m}
    assert by_file["external/x.mp3"]["mean_volume_db"] == pytest.approx(-12.3)
    assert by_file["impact_gen_001.wav"]["mean_volume_db"] is None


# ---------------------------------------------------------------------------
# 編集レシピ: スキーマ / 決定論選択 / mood 重み / 後方互換
# ---------------------------------------------------------------------------

def test_edit_recipes_json_defines_six_recipes_with_required_sections():
    """edit_recipes.json は 6 レシピを定義し、各レシピが必要4セクションを含む。"""
    data = json.loads(_RECIPES_PATH.read_text(encoding="utf-8"))
    recipes = data["recipes"]
    assert set(recipes.keys()) == {"punchy", "minimal", "sparkle", "rhythmic", "dramatic", "clean"}
    for name, body in recipes.items():
        assert "cut_sfx" in body, "recipe {} missing cut_sfx".format(name)
        assert "punch_in" in body, "recipe {} missing punch_in".format(name)
        assert "bgm_curve" in body, "recipe {} missing bgm_curve".format(name)
        assert "first_shot_impact" in body, "recipe {} missing first_shot_impact".format(name)
        # cut_sfx.family_weights は各レシピが必ず自分の重み分布を持つ
        assert isinstance(body["cut_sfx"].get("family_weights"), dict)


def test_edit_recipes_mood_weights_cover_known_moods_and_default():
    """mood_weights が pipeline.bgm_library.KNOWN_MOODS の各 mood + "default" を含む。"""
    data = json.loads(_RECIPES_PATH.read_text(encoding="utf-8"))
    weights = data["mood_weights"]
    for mood in ("upbeat", "calm", "emotional", "dramatic", "lofi", "default"):
        assert mood in weights, "mood {} missing".format(mood)


def test_pick_edit_recipe_deterministic_for_same_seed_and_mood():
    """同じ (seed, mood) は必ず同じレシピを返す。"""
    name_a, _ = edit_profile.pick_edit_recipe("proj-X", "upbeat")
    name_b, _ = edit_profile.pick_edit_recipe("proj-X", "upbeat")
    assert name_a == name_b


def test_pick_edit_recipe_seed_and_mood_change_selection():
    """seed も mood も違えば、レシピが変わる場合がある（何回か試して少なくとも1つは異なる）。"""
    seeds = ["a", "b", "c", "d", "e", "f", "g", "h"]
    picks = {edit_profile.pick_edit_recipe(s, "upbeat")[0] for s in seeds}
    # upbeat では punchy/rhythmic/sparkle/minimal/dramatic のうち複数が候補
    # 8 seed 試して 2 種類以上の結果が出るはず
    assert len(picks) >= 2, "seed variation not producing different recipes: {}".format(picks)


def test_pick_edit_recipe_calm_mood_prefers_minimal_or_clean():
    """calm mood では minimal / clean / sparkle / rhythmic のみが選ばれる（punchy/dramatic は重み0）。"""
    picks = {edit_profile.pick_edit_recipe("seed{}".format(i), "calm")[0] for i in range(20)}
    assert picks
    for name in picks:
        assert name in {"minimal", "clean", "sparkle", "rhythmic"}, name


def test_pick_edit_recipe_returns_none_when_project_seed_is_none():
    """project_seed=None なら後方互換モード（レシピ選択なし）。"""
    name, body = edit_profile.pick_edit_recipe(None, "upbeat")
    assert name is None and body is None


def test_load_edit_profile_backward_compat_without_seed():
    """seed 未指定なら従来の ttp_reference.json 由来の値そのまま。edit_recipe キーは付かない。"""
    profile = edit_profile.load_edit_profile()
    assert "edit_recipe" not in profile
    # ttp_reference の既定値がそのまま反映される
    assert profile["punch_in"]["max_zoom"] == 1.08


def test_load_edit_profile_applies_recipe_when_seed_and_mood_given():
    """seed+mood 指定時はレシピが適用され、edit_recipe キーが付く。"""
    profile = edit_profile.load_edit_profile(project_seed="proj-Y", bgm_mood="upbeat")
    assert profile.get("edit_recipe") in {"punchy", "rhythmic", "sparkle", "minimal", "dramatic"}
    # cut_sfx.family_weights はレシピ由来
    assert isinstance(profile["cut_sfx"].get("family_weights"), dict)


def test_load_edit_profile_recipes_missing_falls_back_to_ttp_reference(tmp_path, monkeypatch):
    """edit_recipes.json が無い環境でも従来どおり ttp_reference edit セクションを使う。"""
    monkeypatch.setattr(edit_profile, "_EDIT_RECIPES_PATH", tmp_path / "missing.json")
    profile = edit_profile.load_edit_profile(project_seed="proj-Z", bgm_mood="upbeat")
    # レシピ選択されず、後方互換のまま
    assert "edit_recipe" not in profile
    # ttp_reference の bgm_curve.hook_gain_db が反映される
    assert profile["bgm_curve"]["hook_gain_db"] == -10


# ---------------------------------------------------------------------------
# density フィルタ (build_edit_cut_sfx_specs)
# ---------------------------------------------------------------------------

def test_build_edit_cut_sfx_specs_density_beats_only_keeps_only_hook_and_cta():
    """density=beats_only は hook_end 付近と cta_start 直前のイベントだけ残す。"""
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": False, "density": "beats_only",
            "resolved_path": "/x.wav", "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    boundaries = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    specs = render.build_edit_cut_sfx_specs(
        boundaries, profile, project_seed="s", hook_end_sec=2.0, cta_start_sec=10.0,
    )
    at_secs = [s["at_sec"] for s in specs]
    # 2.0 (hook_end 直後) と 10.0 (cta_start 直前) の 2 発だけ残る
    assert 2.0 in at_secs
    assert 10.0 in at_secs
    assert len(at_secs) == 2


def test_build_edit_cut_sfx_specs_density_every_n_thins_events():
    """density=every_n は every_n_cuts ごとに1つだけ残す。"""
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": False, "density": "every_n", "every_n_cuts": 2,
            "resolved_path": "/x.wav", "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    boundaries = [0.0, 2.0, 4.0, 6.0, 8.0]  # events = [2, 4, 6, 8]
    specs = render.build_edit_cut_sfx_specs(boundaries, profile, project_seed="s")
    # every_n=2 で index 0,2 だけ残す = 2.0, 6.0
    at_secs = [s["at_sec"] for s in specs]
    assert at_secs == [2.0, 6.0]


def test_build_edit_cut_sfx_specs_first_delay_sec_shifts_first_event_only():
    """first_delay_sec は最初のイベントだけ後ろへずらす（dramatic 演出用）。"""
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": False, "density": "every",
            "first_delay_sec": 0.3,
            "resolved_path": "/x.wav", "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    boundaries = [0.0, 2.0, 4.0, 6.0]
    specs = render.build_edit_cut_sfx_specs(boundaries, profile, project_seed="s")
    # events = [2.0, 4.0, 6.0]、最初だけ +0.3
    at_secs = [s["at_sec"] for s in specs]
    assert at_secs[0] == pytest.approx(2.3)
    assert at_secs[1] == pytest.approx(4.0)
    assert at_secs[2] == pytest.approx(6.0)


def test_build_edit_cut_sfx_specs_recipe_family_weights_override_position_based(monkeypatch):
    """recipe が cut_sfx.family_weights を持つと位置別重み(_family_weights_for_event)より優先される。"""
    fake_manifest = [
        {"file": "shimmer_gen_001.wav", "family": "shimmer", "tags": ["shimmer"], "source": None},
        {"file": "shimmer_gen_002.wav", "family": "shimmer", "tags": ["shimmer"], "source": None},
        {"file": "impact_gen_001.wav", "family": "impact", "tags": ["impact"], "source": None},
    ]
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name) if name else None,
    )
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "density": "every",
            "family_weights": {"shimmer": 10},  # レシピ由来: shimmer だけ許可
            "manifest": fake_manifest,
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    # フック位置(hook_end_sec=2.0) 直後は本来 impact が優先されるが、
    # レシピの family_weights が shimmer のみを許可するため shimmer が選ばれる。
    specs = render.build_edit_cut_sfx_specs(
        [0.0, 2.0], profile, project_seed="seedR", hook_end_sec=2.0, cta_start_sec=5.0,
    )
    assert len(specs) == 1
    assert "shimmer_" in Path(specs[0]["path"]).name


# ---------------------------------------------------------------------------
# jobs.py 経由の project["edit_recipe"] 記録
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_projects(tmp_path, monkeypatch):
    from studio.server import projects, jobs as jobs_mod
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield projects, jobs_mod


class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 5.0, "is_silent": False}


def _make_project_with_mood(projects, mood):
    project = projects.create_project("レシピ配線テスト", 10.0, "mock", status="draft")
    project["bgm_mood"] = mood
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for idx, (start, end) in enumerate([(0.0, 2.0), (0.0, 3.0), (0.0, 2.5)], start=1):
        sid = "s{}".format(idx)
        (clips_dir / "{}.mp4".format(sid)).write_bytes(b"\x00")
        shots.append({
            "id": sid, "order": idx - 1, "enabled": True, "prompt": "abstract",
            "caption": "テスト{}".format(idx),
            "clip_path": projects.media_relpath_for_clip(project["id"], "{}.mp4".format(sid)),
            "source_duration": end, "trim": {"start": start, "end": end},
        })
    project["plan"] = {
        "shots": shots, "narration_text": "ナレ",
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)
    return project


def test_render_project_records_edit_recipe_on_project(_isolated_projects, monkeypatch):
    """_render_project は edit_prof.edit_recipe を project["edit_recipe"] に記録する。"""
    projects, jobs_mod = _isolated_projects
    project = _make_project_with_mood(projects, "upbeat")

    calls = []

    def fake_run_ffmpeg(cmd, timeout_sec=None):
        calls.append(cmd)
        return {"returncode": 0, "stderr": "", "stdout": ""}

    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full modeのみ")),
    )
    monkeypatch.setattr(
        jobs_mod.tts_mod, "get_tts_backend",
        lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend(),
    )

    jobs_mod._render_project(project["id"], project["plan"], {"ffmpeg_bin": "/bin/ffmpeg"})

    saved = projects.get_project(project["id"])
    # レシピが選ばれているはず (upbeat の重みで punchy/rhythmic/sparkle 等)
    assert saved.get("edit_recipe") in {"punchy", "rhythmic", "sparkle", "minimal", "dramatic"}
    assert saved.get("edit_profile_applied") is True


def test_render_project_without_bgm_mood_uses_default_weights(_isolated_projects, monkeypatch):
    """bgm_mood 未設定でも、"default" 重みでレシピが選ばれる（後方互換ではなく既定）。
    ただし直後にレシピが記録されるだけで、レンダは失敗せず完走する。
    """
    projects, jobs_mod = _isolated_projects
    # mood 未指定
    project = projects.create_project("mood無しテスト", 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    for idx, (start, end) in enumerate([(0.0, 2.0), (0.0, 3.0)], start=1):
        sid = "s{}".format(idx)
        (clips_dir / "{}.mp4".format(sid)).write_bytes(b"\x00")
    project["plan"] = {
        "shots": [
            {"id": "s1", "order": 0, "enabled": True, "prompt": "a", "caption": "1",
             "clip_path": projects.media_relpath_for_clip(project["id"], "s1.mp4"),
             "source_duration": 2.0, "trim": {"start": 0.0, "end": 2.0}},
            {"id": "s2", "order": 1, "enabled": True, "prompt": "a", "caption": "2",
             "clip_path": projects.media_relpath_for_clip(project["id"], "s2.mp4"),
             "source_duration": 3.0, "trim": {"start": 0.0, "end": 3.0}},
        ],
        "narration_text": "テスト", "bgm": None, "sfx": [],
        "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    projects.save_project(project)

    monkeypatch.setattr(
        jobs_mod.render, "run_ffmpeg",
        lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": "", "stdout": ""},
    )
    monkeypatch.setattr(
        jobs_mod.tts_mod, "synthesize_segments",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full")),
    )
    monkeypatch.setattr(
        jobs_mod.tts_mod, "get_tts_backend",
        lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend(),
    )

    jobs_mod._render_project(project["id"], project["plan"], {"ffmpeg_bin": "/bin/ffmpeg"})

    saved = projects.get_project(project["id"])
    # mood 無し → default 重みで6種のいずれかが選ばれる
    assert saved.get("edit_recipe") in {"punchy", "minimal", "sparkle", "rhythmic", "dramatic", "clean"}
