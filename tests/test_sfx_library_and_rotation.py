# -*- coding: utf-8 -*-
"""SFXライブラリ（assets/sfx/generate_sfx_library.py）とローテーション選択エンジン
（pipeline.edit_profile.pick_cut_sfx / render.build_edit_cut_sfx_specs のローテーションモード）
のテスト。

生成関数の実 ffmpeg 実行は「1ファミリー1個」だけスモークとして走らせる（フルライブラリ
生成は開発者が assets/sfx/generate_sfx_library.py を手動実行して asset にコミットする前提）。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import edit_profile
from pipeline import render

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GEN_SCRIPT = _REPO_ROOT / "assets" / "sfx" / "generate_sfx_library.py"
_FFPROBE = _REPO_ROOT / "bin" / "ffprobe"


def _load_generate_module():
    spec = importlib.util.spec_from_file_location("generate_sfx_library", _GEN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _require_generated_sfx():
    """assets/sfx/*_gen_*.wav が未生成なら skip する（generate_sfx_library.py を先に走らせる）。

    生成物(*_gen_*.wav)は .gitignore で非コミット。starter側と同じスキップパターン。
    """
    sfx_dir = _REPO_ROOT / "assets" / "sfx"
    if not any(sfx_dir.glob("*_gen_*.wav")):
        pytest.skip("SFX生成物が未生成のため（先に assets/sfx/generate_sfx_library.py を実行）")


# ---------------------------------------------------------------------------
# 生成スクリプトの静的検証（実 ffmpeg を走らせない範囲）
# ---------------------------------------------------------------------------

def test_generate_sfx_library_has_five_families_and_expected_counts():
    """生成spec上のファミリー数と各件数（合計約110）を静的に検証する。"""
    gsl = _load_generate_module()
    specs = gsl.all_specs()
    from collections import Counter
    counts = Counter(s["family"] for s in specs)
    assert counts["whoosh"] == 30
    assert counts["pop"] == 25
    assert counts["riser"] == 20
    assert counts["impact"] == 20
    assert counts["shimmer"] == 15
    assert sum(counts.values()) >= 100  # 目標: 100以上
    assert sum(counts.values()) <= 140  # 目標上限


def test_generate_sfx_library_legacy_entries_are_included_in_manifest_scheme():
    """_legacy_manifest_entries() は 8種のlegacyファイルをfamily='legacy'で返す。"""
    gsl = _load_generate_module()
    entries = gsl._legacy_manifest_entries()
    assert len(entries) == 8
    for e in entries:
        assert e["family"] == "legacy"
        assert e["file"].endswith(".wav")


def test_committed_manifest_has_100_plus_entries_and_all_families():
    """コミット済みの assets/sfx/manifest.json が100件以上・全ファミリー入りであることを確認する。"""
    _require_generated_sfx()
    manifest_path = _REPO_ROOT / "assets" / "sfx" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest) >= 100
    families = {e.get("family") for e in manifest if isinstance(e, dict)}
    assert {"whoosh", "pop", "riser", "impact", "shimmer", "legacy"}.issubset(families)


def test_committed_manifest_files_exist_and_are_wav():
    """manifest.json に載っている file が全部 assets/sfx/ に実在する。"""
    _require_generated_sfx()
    manifest_path = _REPO_ROOT / "assets" / "sfx" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for e in manifest:
        p = _REPO_ROOT / "assets" / "sfx" / e["file"]
        assert p.exists(), "missing: {}".format(p)
        assert p.suffix == ".wav"


def test_generate_one_smoke_actually_runs_ffmpeg(tmp_path):
    """実 ffmpeg で1個だけ生成し、ffprobe で尺が取れることを確認する（配線スモーク）。"""
    gsl = _load_generate_module()
    spec = gsl._whoosh_specs()[0]  # 最短の 0.25s
    entry = gsl.generate_one(spec, tmp_path, index_in_family=1)
    out_path = tmp_path / entry["file"]
    assert out_path.exists()
    # ffprobe で尺を確認（0.15〜0.8s の範囲内）
    proc = subprocess.run(
        [str(_FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    dur = float(proc.stdout.decode().strip())
    assert 0.10 < dur < 0.9


# ---------------------------------------------------------------------------
# pick_cut_sfx: 決定論・連続回避・重み尊重
# ---------------------------------------------------------------------------

def _fake_manifest():
    return [
        {"file": "whoosh_a.wav", "family": "whoosh", "tags": ["transition"]},
        {"file": "whoosh_b.wav", "family": "whoosh", "tags": ["transition"]},
        {"file": "whoosh_c.wav", "family": "whoosh", "tags": ["transition"]},
        {"file": "pop_a.wav", "family": "pop", "tags": ["accent"]},
        {"file": "pop_b.wav", "family": "pop", "tags": ["accent"]},
        {"file": "impact_a.wav", "family": "impact", "tags": ["impact"]},
        {"file": "impact_b.wav", "family": "impact", "tags": ["impact"]},
        {"file": "riser_a.wav", "family": "riser", "tags": ["riser"]},
        {"file": "shimmer_a.wav", "family": "shimmer", "tags": ["shimmer"]},
    ]


def test_pick_cut_sfx_returns_none_for_empty_manifest():
    assert edit_profile.pick_cut_sfx("seed1", 0, []) is None


def test_pick_cut_sfx_is_deterministic_for_same_seed_and_index():
    m = _fake_manifest()
    a = edit_profile.pick_cut_sfx("proj-A", 3, m)
    b = edit_profile.pick_cut_sfx("proj-A", 3, m)
    assert a == b


def test_pick_cut_sfx_differs_across_seeds():
    """同じindex, 異なるseed → 全ての index で同一結果ということはあり得ない
    （少なくとも1つは異なる）。"""
    m = _fake_manifest()
    results_a = [edit_profile.pick_cut_sfx("seed-A", i, m)["file"] for i in range(6)]
    results_b = [edit_profile.pick_cut_sfx("seed-B", i, m)["file"] for i in range(6)]
    assert results_a != results_b


def test_pick_cut_sfx_avoids_consecutive_repeat():
    """avoid_file で指定したファイルは、候補が他にあれば必ず選ばれない。"""
    m = _fake_manifest()
    # 何度実行しても avoid_file と一致してはいけない
    for i in range(20):
        picked = edit_profile.pick_cut_sfx("seed", i, m, avoid_file="whoosh_a.wav")
        assert picked["file"] != "whoosh_a.wav"


def test_pick_cut_sfx_respects_family_weights_heavy_family_dominates():
    """impact 重み10, 他は0 → 常に impact 系が選ばれる。"""
    m = _fake_manifest()
    weights = {"impact": 10}
    for i in range(30):
        picked = edit_profile.pick_cut_sfx("s", i, m, family_weights=weights)
        assert picked["family"] == "impact"


def test_pick_cut_sfx_falls_back_when_requested_family_absent_from_manifest():
    """存在しないファミリーだけを重み付けしても、他ファミリーからフォールバックで選ばれる
    （空リストで詰まらせない安全策）。"""
    m = _fake_manifest()
    weights = {"nonexistent_family": 5}
    picked = edit_profile.pick_cut_sfx("s", 0, m, family_weights=weights)
    assert picked is not None
    assert picked["family"] in {"whoosh", "pop", "impact", "riser", "shimmer"}


# ---------------------------------------------------------------------------
# render.build_edit_cut_sfx_specs のローテーションモード（後方互換 + 新機能）
# ---------------------------------------------------------------------------

def test_build_edit_cut_sfx_specs_fixed_file_backward_compat_when_rotate_false():
    """rotate=False（=ttp_reference.json で file 明示）→ 従来どおり resolved_path 固定。"""
    profile = {
        "cut_sfx": {
            "enabled": True,
            "rotate": False,
            "resolved_path": "/assets/sfx/whoosh_01.wav",
            "gain_db": -18,
            "min_interval_sec": 1.5,
        }
    }
    specs = render.build_edit_cut_sfx_specs([0.0, 3.0, 6.0], profile)
    assert len(specs) == 2
    assert all(s["path"] == "/assets/sfx/whoosh_01.wav" for s in specs)


def test_build_edit_cut_sfx_specs_rotation_uses_manifest_and_avoids_consecutive(monkeypatch):
    """rotate=True + manifest → 各イベントで異なるSFXファイルへ decay させ、
    連続同音を避ける（隣接するイベント同士のパスは必ず違う）。"""
    fake_manifest = _fake_manifest()
    # 実パス解決をモック（テスト用manifestの file 名は実在しないため）
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name) if name else None,
    )
    profile = {
        "cut_sfx": {
            "enabled": True,
            "rotate": True,
            "manifest": fake_manifest,
            "resolved_path": None,
            "gain_db": -20,
            "min_interval_sec": 1.0,
        }
    }
    # 5カット
    boundaries = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    specs = render.build_edit_cut_sfx_specs(boundaries, profile, project_seed="proj-X")
    assert len(specs) == 5
    # 隣接するイベント同士は違うファイル
    for i in range(1, len(specs)):
        assert specs[i]["path"] != specs[i - 1]["path"]
    # gain_db が伝播
    assert all(s["gain_db"] == -20 for s in specs)


def test_build_edit_cut_sfx_specs_rotation_deterministic_across_runs(monkeypatch):
    """同じ seed / manifest / boundaries なら 2 回呼んでも完全に同じ specs を返す。"""
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name) if name else None,
    )
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "manifest": fake_manifest,
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    boundaries = [0.0, 2.0, 4.0, 6.0, 8.0]
    a = render.build_edit_cut_sfx_specs(boundaries, profile, project_seed="same-seed")
    b = render.build_edit_cut_sfx_specs(boundaries, profile, project_seed="same-seed")
    assert a == b


def test_build_edit_cut_sfx_specs_rotation_beat_weights_prefer_impact_after_hook(monkeypatch):
    """フック直後（hook_end_sec 付近のイベント）は impact 系が優先される。"""
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name),
    )
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "manifest": fake_manifest,
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    # boundaries[1] = 2.0 が hook 終わり = 最初のカット位置
    specs = render.build_edit_cut_sfx_specs(
        [0.0, 2.0, 5.0, 8.0, 10.0], profile,
        project_seed="seedZ", hook_end_sec=2.0, cta_start_sec=10.0,
    )
    # 最初のイベント(at_sec=2.0)は hook_end 付近 → impact family
    assert "impact_" in Path(specs[0]["path"]).name


def test_build_edit_cut_sfx_specs_rotation_beat_weights_prefer_riser_before_cta(monkeypatch):
    """CTA直前（cta_start_sec 直前1.5秒以内のイベント）は riser 系が優先される。"""
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(
        edit_profile, "resolve_sfx_file_path",
        lambda name: "/fake/sfx/{}".format(name),
    )
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "manifest": fake_manifest,
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    # cta_start_sec=8.0 → 最後の境界7.0は cta_start-1.5=6.5以上 → riser優先
    specs = render.build_edit_cut_sfx_specs(
        [0.0, 2.0, 4.0, 7.0], profile,
        project_seed="seedR", hook_end_sec=2.0, cta_start_sec=8.0,
    )
    # 最後のイベント(at_sec=7.0)は CTA直前 → riser family
    assert "riser_" in Path(specs[-1]["path"]).name


def test_build_edit_cut_sfx_specs_rotation_falls_back_to_resolved_path_when_manifest_empty():
    """rotate=True でも manifest が空なら resolved_path フォールバック（=固定モード相当）。"""
    profile = {
        "cut_sfx": {
            "enabled": True, "rotate": True, "manifest": [],
            "resolved_path": "/legacy/whoosh_01.wav",
            "gain_db": -18, "min_interval_sec": 1.0,
        }
    }
    specs = render.build_edit_cut_sfx_specs([0.0, 3.0, 6.0], profile, project_seed="s")
    assert all(s["path"] == "/legacy/whoosh_01.wav" for s in specs)


# ---------------------------------------------------------------------------
# load_edit_profile: manifest / rotate フラグの添付
# ---------------------------------------------------------------------------

def test_load_edit_profile_attaches_manifest_and_rotate_flag_when_file_not_specified(
    tmp_path, monkeypatch,
):
    """ttp_reference.json の edit.cut_sfx に file 指定が無い → rotate=True + manifest 添付。"""
    ref_path = tmp_path / "ttp_reference.json"
    ref_path.write_text(
        json.dumps({"edit": {"cut_sfx": {"enabled": True, "gain_db": -18}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["rotate"] is True
    assert isinstance(profile["cut_sfx"]["manifest"], list)
    assert len(profile["cut_sfx"]["manifest"]) >= 8  # legacyだけでも8


def test_load_edit_profile_disables_rotate_when_file_explicitly_set(tmp_path, monkeypatch):
    """ttp_reference.json の edit.cut_sfx.file が明示指定 → rotate=False（固定モード）。"""
    ref_path = tmp_path / "ttp_reference.json"
    ref_path.write_text(
        json.dumps({"edit": {"cut_sfx": {"file": "swoosh_01.wav"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(edit_profile, "_TTP_REFERENCE_PATH", ref_path)

    profile = edit_profile.load_edit_profile()

    assert profile["cut_sfx"]["rotate"] is False
    assert profile["cut_sfx"]["file"] == "swoosh_01.wav"


def test_load_sfx_manifest_normalizes_legacy_entries_without_family():
    """family キーが無い旧形式でも family='legacy' として正規化される（後方互換）。"""
    manifest = edit_profile.load_sfx_manifest()
    for e in manifest:
        assert "family" in e
        assert "tags" in e
        assert isinstance(e["tags"], list)
        assert e["file"]


def test_compute_edit_enhancement_kwargs_accepts_project_seed_kwarg(monkeypatch):
    """新しい project_seed kwarg を受け付け、ローテーション時に seed が伝播する。"""
    # 実 manifest を使う（既にコミット済み）
    profile = edit_profile.load_edit_profile()
    kwargs_a = render.compute_edit_enhancement_kwargs(
        [2.0, 3.0, 2.0, 4.0], profile, project_seed="proj-alpha",
    )
    kwargs_b = render.compute_edit_enhancement_kwargs(
        [2.0, 3.0, 2.0, 4.0], profile, project_seed="proj-beta",
    )
    paths_a = [s["path"] for s in kwargs_a["sfx_extra"]]
    paths_b = [s["path"] for s in kwargs_b["sfx_extra"]]
    # 少なくとも1件は違う（seed違いで並びが変わる）
    assert paths_a != paths_b


# ---------------------------------------------------------------------------
# 独立レビュー修正: pick_cut_sfx の avoid_file フォールバック（家族またぎ）
# ---------------------------------------------------------------------------

def test_pick_cut_sfx_avoid_file_falls_through_to_next_family():
    """単一候補ファミリー + そのファイルが avoid_file なら次ファミリーへフォールバックする。

    修正前: そのファミリー内で avoid_file と全衝突すると candidates[0] （=avoid_file）
    を返してしまい、連続同音回避が機能しなかった。
    """
    # whoosh は 1件のみ・その1件を avoid_file にする → 次ファミリー(pop)から選ばれるべき
    manifest = [
        {"file": "whoosh_only.wav", "family": "whoosh", "tags": ["transition"]},
        {"file": "pop_a.wav", "family": "pop", "tags": ["accent"]},
        {"file": "pop_b.wav", "family": "pop", "tags": ["accent"]},
    ]
    # whoosh重み最大 → まず whoosh が試される → avoid_file と衝突 → pop へフォールバック
    weights = {"whoosh": 10, "pop": 3}
    picked = edit_profile.pick_cut_sfx(
        "seed-fb", 0, manifest,
        family_weights=weights, avoid_file="whoosh_only.wav",
    )
    assert picked is not None
    assert picked["file"] != "whoosh_only.wav"
    assert picked["family"] == "pop"


# ---------------------------------------------------------------------------
# 独立レビュー修正: generate_sfx_library.py の manifest upsert
# ---------------------------------------------------------------------------

def test_generate_sfx_library_manifest_upsert_preserves_manual_entries(
    tmp_path, monkeypatch,
):
    """既存 manifest.json に手動追記された file 名エントリは、再生成しても保全される。"""
    gsl = _load_generate_module()
    # 事前に「手動追記」を含む manifest を tmp_path に配置
    manual_entry = {
        "file": "custom_hand_edited.wav",
        "label_jp": "手動追記SE",
        "family": "custom",
        "tags": ["manual"],
        "duration": 0.5,
        "license": "manual",
    }
    # legacy 8種 + 手動1 で下地を作る
    pre_manifest = gsl._legacy_manifest_entries() + [manual_entry]
    (tmp_path / "manifest.json").write_text(
        json.dumps(pre_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    # 実 ffmpeg は走らせず generate_one をモックし、write_manifest だけを検証
    monkeypatch.setattr(
        gsl, "generate_one",
        lambda spec, out_dir, index_in_family: {
            "file": "{}_gen_{:03d}.wav".format(spec["family"], index_in_family),
            "label_jp": "mocked",
            "family": spec["family"],
            "tags": list(spec["tags"]),
            "duration": spec["duration"],
            "license": "mocked",
        },
    )
    result = gsl.main(out_dir=tmp_path, families=["pop"], write_manifest=True)

    files = [e["file"] for e in result]
    assert "custom_hand_edited.wav" in files, "手動追記エントリが upsert で失われた"

    # ディスクからも読み直して同じであることを確認
    reloaded = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert any(e.get("file") == "custom_hand_edited.wav" for e in reloaded)
    # pop の生成エントリが少なくとも1件は追加されていること
    assert any(e.get("family") == "pop" and e.get("file", "").endswith(".wav") for e in reloaded)
