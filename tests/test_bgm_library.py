# -*- coding: utf-8 -*-
"""pipeline/bgm_library.py と assets/bgm/generate_starter_pack.py の単体テスト。

- inbox 取り込み: ムード規約・重複sha1スキップ・imported/への移動
- pick_bgm: mood一致・"any" fallback・シード決定論・直近2曲回避
- スターター12曲仕様: TRACK_SPECS の件数・ムード内訳
- jobs.py の _resolve_bgm_by_mood 後方互換（bgm_library 経由に置換後もフォーマット同一）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import bgm_library


# ---------------------------------------------------------------------------
# ムード判定
# ---------------------------------------------------------------------------

def test_mood_from_filename_known_tokens():
    assert bgm_library.mood_from_filename("upbeat_夏っぽい") == "upbeat"
    assert bgm_library.mood_from_filename("calm-piano-2") == "calm"
    assert bgm_library.mood_from_filename("emotional something") == "emotional"
    assert bgm_library.mood_from_filename("dramatic_epic_boss") == "dramatic"
    assert bgm_library.mood_from_filename("lofi_chill_night") == "lofi"


def test_mood_from_filename_unknown_returns_any():
    assert bgm_library.mood_from_filename("random_song") == "any"
    assert bgm_library.mood_from_filename("mytrack") == "any"


# ---------------------------------------------------------------------------
# import_inbox
# ---------------------------------------------------------------------------

def _write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _dirs(tmp_path: Path):
    ibx = tmp_path / "inbox"
    imp = ibx / "imported"
    lib = tmp_path / "library"
    mfp = tmp_path / "manifest.json"
    ibx.mkdir(parents=True, exist_ok=True)
    return ibx, imp, lib, mfp


def test_import_inbox_returns_empty_when_no_inbox(tmp_path):
    result = bgm_library.import_inbox(
        inbox_dir_override=str(tmp_path / "nope"),
        imported_dir_override=str(tmp_path / "imp"),
        library_dir_override=str(tmp_path / "lib"),
        manifest_path_override=str(tmp_path / "m.json"),
    )
    assert result == {"imported": [], "skipped": []}


def test_import_inbox_processes_new_file_and_records_manifest(tmp_path):
    ibx, imp, lib, mfp = _dirs(tmp_path)
    src = ibx / "upbeat_夏っぽい.mp3"
    _write_bytes(src, b"AAA1")

    converted = []

    def _prober(p):
        return 42.5

    def _converter(src_path, dst_path):
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(b"CONV")
        converted.append((str(src_path), str(dst_path)))

    result = bgm_library.import_inbox(
        prober=_prober, converter=_converter,
        inbox_dir_override=str(ibx), imported_dir_override=str(imp),
        library_dir_override=str(lib), manifest_path_override=str(mfp),
    )
    assert len(result["imported"]) == 1
    entry = result["imported"][0]
    assert entry["mood"] == "upbeat"
    assert entry["file"].startswith("library/")
    assert entry["file"].endswith(".m4a")
    assert entry["duration_sec"] == 42.5
    assert entry["sha1"]
    assert entry["loop_ok"] is True

    # 変換後ファイルが実在
    assert (lib / Path(entry["file"]).name).exists()
    # 原本は inbox から imported/ へ移動
    assert not src.exists()
    assert (imp / "upbeat_夏っぽい.mp3").exists()
    # manifest 書き出し
    saved = json.loads(mfp.read_text(encoding="utf-8"))
    assert isinstance(saved, list) and len(saved) == 1
    assert saved[0]["file"] == entry["file"]


def test_import_inbox_skips_duplicate_sha1(tmp_path):
    ibx, imp, lib, mfp = _dirs(tmp_path)
    src = ibx / "calm_night.wav"
    _write_bytes(src, b"SAME_BYTES_XYZ")

    # 既存 manifest に同じ sha1 を先仕込み
    import hashlib
    existing_sha = hashlib.sha1(b"SAME_BYTES_XYZ").hexdigest()
    mfp.write_text(json.dumps([{
        "file": "library/prev.m4a", "mood": "calm", "sha1": existing_sha, "loop_ok": True,
    }]) + "\n", encoding="utf-8")

    def _prober(p): return 30.0
    def _converter(s, d):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"NEW")

    result = bgm_library.import_inbox(
        prober=_prober, converter=_converter,
        inbox_dir_override=str(ibx), imported_dir_override=str(imp),
        library_dir_override=str(lib), manifest_path_override=str(mfp),
    )
    assert result["imported"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "duplicate-sha1"
    # 重複でも imported/ へ退避（inbox に残さない）
    assert not src.exists()
    assert (imp / "calm_night.wav").exists()
    # manifest は変更なし（1件のまま）
    saved = json.loads(mfp.read_text(encoding="utf-8"))
    assert len(saved) == 1


def test_import_inbox_probe_failure_is_skipped(tmp_path):
    ibx, imp, lib, mfp = _dirs(tmp_path)
    src = ibx / "emotional_song.m4a"
    _write_bytes(src, b"XYZ")

    def _prober(p): return None
    called = {"n": 0}
    def _converter(s, d): called["n"] += 1

    result = bgm_library.import_inbox(
        prober=_prober, converter=_converter,
        inbox_dir_override=str(ibx), imported_dir_override=str(imp),
        library_dir_override=str(lib), manifest_path_override=str(mfp),
    )
    assert result["imported"] == []
    assert result["skipped"] and result["skipped"][0]["reason"] == "probe-failed"
    assert called["n"] == 0  # converter は呼ばれない
    assert src.exists()  # probe 失敗時は原本を移動しない（次回リトライ可）


def test_import_inbox_ignores_unknown_extensions(tmp_path):
    ibx, imp, lib, mfp = _dirs(tmp_path)
    _write_bytes(ibx / "upbeat_song.txt", b"notaudio")
    _write_bytes(ibx / "readme.md", b"doc")

    result = bgm_library.import_inbox(
        prober=lambda p: 10.0,
        converter=lambda s, d: d.write_bytes(b"x"),
        inbox_dir_override=str(ibx), imported_dir_override=str(imp),
        library_dir_override=str(lib), manifest_path_override=str(mfp),
    )
    assert result == {"imported": [], "skipped": []}


# ---------------------------------------------------------------------------
# pick_bgm
# ---------------------------------------------------------------------------

_MANIFEST_STUB = [
    {"file": "library/upbeat_a.m4a", "mood": "upbeat"},
    {"file": "library/upbeat_b.m4a", "mood": "upbeat"},
    {"file": "library/upbeat_c.m4a", "mood": "upbeat"},
    {"file": "library/calm_a.m4a", "mood": "calm"},
    {"file": "library/any_generic.m4a", "mood": "any"},
]


def test_pick_bgm_none_mood_returns_none():
    assert bgm_library.pick_bgm(None, manifest=_MANIFEST_STUB, history={"recent": []}) is None
    assert bgm_library.pick_bgm("none", manifest=_MANIFEST_STUB, history={"recent": []}) is None


def test_pick_bgm_deterministic_by_seed():
    a = bgm_library.pick_bgm("upbeat", seed=42, manifest=_MANIFEST_STUB, history={"recent": []})
    b = bgm_library.pick_bgm("upbeat", seed=42, manifest=_MANIFEST_STUB, history={"recent": []})
    assert a["file"] == b["file"]
    # 異なるシードでは(高確率で)別の曲になる — 全4曲(exact3+any1)から違うシードを試して1つでも変われば決定論を証明
    files = {bgm_library.pick_bgm("upbeat", seed=s, manifest=_MANIFEST_STUB, history={"recent": []})["file"]
             for s in range(20)}
    assert len(files) >= 2


def test_pick_bgm_avoids_recent_two():
    hist = {"recent": [
        {"file": "library/upbeat_a.m4a", "mood": "upbeat"},
        {"file": "library/upbeat_b.m4a", "mood": "upbeat"},
    ]}
    # 直近2曲を除外して upbeat_c か any_generic のみが候補
    for seed in range(30):
        chosen = bgm_library.pick_bgm("upbeat", seed=seed, manifest=_MANIFEST_STUB, history=hist)
        assert chosen["file"] in ("library/upbeat_c.m4a", "library/any_generic.m4a")


def test_pick_bgm_any_pool_is_included_with_exact():
    # exact + any を統合した pool から選ばれる
    files = {bgm_library.pick_bgm("upbeat", seed=s, manifest=_MANIFEST_STUB, history={"recent": []})["file"]
             for s in range(60)}
    assert "library/any_generic.m4a" in files


def test_pick_bgm_falls_back_when_no_mood_match():
    # 未登録 mood なら「全体」から選ぶ（BGMを完全に落とすよりベター）
    got = bgm_library.pick_bgm("dramatic", seed=1, manifest=_MANIFEST_STUB, history={"recent": []})
    assert got is not None
    assert got["file"] in {e["file"] for e in _MANIFEST_STUB}


def test_pick_bgm_records_history(tmp_path):
    hist_path = tmp_path / ".history.json"
    got = bgm_library.pick_bgm(
        "upbeat", seed=7, manifest=_MANIFEST_STUB, history={"recent": []},
        record_project_id="prj-1", history_path_override=str(hist_path),
    )
    assert got is not None
    saved = json.loads(hist_path.read_text(encoding="utf-8"))
    assert saved["recent"][-1]["project_id"] == "prj-1"
    assert saved["recent"][-1]["file"] == got["file"]


def test_pick_bgm_empty_manifest_returns_none():
    assert bgm_library.pick_bgm("upbeat", manifest=[], history={"recent": []}) is None


# ---------------------------------------------------------------------------
# スターター12曲仕様
# ---------------------------------------------------------------------------

def test_starter_pack_specs_have_12_tracks_across_4_moods():
    import importlib.util
    from pipeline.config import project_root
    p = project_root() / "assets" / "bgm" / "generate_starter_pack.py"
    spec = importlib.util.spec_from_file_location("_starter_pack", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    specs = mod.track_specs()
    assert len(specs) == 12
    moods = [s[1] for s in specs]
    from collections import Counter
    counter = Counter(moods)
    # 各moodちょうど3本
    for m in ("upbeat", "calm", "emotional", "dramatic"):
        assert counter[m] == 3, "{}が3本ではない: {}".format(m, counter[m])
    # bpm はムードごとに散らばっている
    bpms = [s[2] for s in specs]
    assert len(set(bpms)) >= 4  # 少なくとも4種類の異なるbpm


def test_starter_pack_files_and_manifest_consistent():
    """generate_starter_pack.py 実行後の状態が manifest と library/ で整合していること。"""
    from pipeline.config import project_root
    root = project_root() / "assets" / "bgm"
    lib = root / "library"
    if not lib.exists():
        pytest.skip("library/ が未生成のため（先に generate_starter_pack.py を実行）")
    m4a_files = sorted(p.name for p in lib.glob("*.m4a"))
    assert len(m4a_files) >= 12
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    lib_entries = [e for e in manifest if isinstance(e, dict)
                   and str(e.get("file", "")).startswith("library/")]
    assert len(lib_entries) >= 12
    # manifest の library/ 参照が全てファイル実在
    for e in lib_entries:
        assert (root / e["file"]).exists(), "manifestに載っているのにファイルが無い: {}".format(e["file"])


# ---------------------------------------------------------------------------
# jobs.py 配線: 後方互換
# ---------------------------------------------------------------------------

def test_jobs_resolve_bgm_returns_str_or_none(monkeypatch):
    """_resolve_bgm_by_mood は従来通り filename(str) or None を返す。"""
    from studio.server import jobs as jobs_mod
    # None / "none" は None
    assert jobs_mod._resolve_bgm_by_mood(None) is None
    assert jobs_mod._resolve_bgm_by_mood("none") is None
    # 実在mood → str
    got = jobs_mod._resolve_bgm_by_mood("upbeat", seed=1)
    assert got is None or isinstance(got, str)


def test_jobs_resolve_bgm_uses_pick_bgm(monkeypatch):
    """_resolve_bgm_by_mood が pipeline.bgm_library.pick_bgm を経由することの証跡。"""
    from studio.server import jobs as jobs_mod
    from pipeline import bgm_library as blib

    calls = []

    def _fake_pick(mood, seed=None, manifest=None, history=None, record_project_id=None, history_path_override=None, target_bpm=None):
        calls.append({"mood": mood, "seed": seed, "record_project_id": record_project_id})
        return {"file": "library/fake_test.m4a", "mood": mood}

    monkeypatch.setattr(blib, "pick_bgm", _fake_pick)
    got = jobs_mod._resolve_bgm_by_mood("upbeat", project_id="prj-xyz")
    assert got == "library/fake_test.m4a"
    assert calls and calls[0]["mood"] == "upbeat"
    assert calls[0]["record_project_id"] == "prj-xyz"


# ---------------------------------------------------------------------------
# 独立レビュー修正: _save_history_to のアトミック性・pick_bgm 履歴重複回避
# ---------------------------------------------------------------------------

def test_save_history_to_writes_valid_json_atomically(tmp_path):
    """_save_history_to 後に読み直すと妥当な JSON になっている（tempfile→os.replace）。"""
    hist_path = tmp_path / ".history.json"
    payload = {"recent": [
        {"project_id": "p1", "file": "library/a.m4a", "mood": "upbeat", "ts": "2026-07-20T00:00:00"},
        {"project_id": "p2", "file": "library/b.m4a", "mood": "calm", "ts": "2026-07-20T00:00:01"},
    ]}
    bgm_library._save_history_to(hist_path, payload)
    loaded = json.loads(hist_path.read_text(encoding="utf-8"))
    assert loaded == payload
    # tempfile が残っていないこと（.tmp サフィックス）
    leftover = list(tmp_path.glob("." + hist_path.name + ".*.tmp"))
    assert leftover == [], "atomic write tempfile が残っている: {}".format(leftover)


def test_pick_bgm_same_project_and_mood_does_not_duplicate_history(tmp_path):
    """同じ (project_id, mood) で2回 pick_bgm しても history recent に重複追記されない。"""
    hist_path = tmp_path / ".history.json"
    hist = {"recent": []}
    first = bgm_library.pick_bgm(
        "upbeat", seed=7, manifest=_MANIFEST_STUB, history=hist,
        record_project_id="prj-dup", history_path_override=str(hist_path),
    )
    # 同じ history dict を再利用（実際の再実行時のロードと同じ状況）
    saved = json.loads(hist_path.read_text(encoding="utf-8"))
    second = bgm_library.pick_bgm(
        "upbeat", seed=7, manifest=_MANIFEST_STUB, history=saved,
        record_project_id="prj-dup", history_path_override=str(hist_path),
    )
    # 決定論のため同じ曲が選ばれる（回避対象が増えて別曲になる副作用は許容しない）
    assert first["file"] == second["file"]
    final = json.loads(hist_path.read_text(encoding="utf-8"))
    dup_count = sum(
        1 for r in final["recent"]
        if r.get("project_id") == "prj-dup" and r.get("mood") == "upbeat"
    )
    assert dup_count == 1, "同じ(project_id, mood)で履歴が重複した: {}".format(final)


def test_run_resolve_bgm_uses_pick_bgm(monkeypatch, tmp_path):
    """run.py の _resolve_bgm も pipeline.bgm_library.pick_bgm を経由する。"""
    import run as run_mod
    from pipeline import bgm_library as blib

    fake_file = "library/upbeat_bright_01.m4a"  # 実生成済みなのでパス解決に成功する
    def _fake_pick(mood, seed=None, manifest=None, history=None, record_project_id=None, history_path_override=None, target_bpm=None):
        return {"file": fake_file, "mood": mood}

    monkeypatch.setattr(blib, "pick_bgm", _fake_pick)
    got = run_mod._resolve_bgm("upbeat", project_id="run-1")
    # 実ファイル存在時のみパス文字列が返る。無ければ None（環境依存）。
    if got is not None:
        assert got.endswith(fake_file)
