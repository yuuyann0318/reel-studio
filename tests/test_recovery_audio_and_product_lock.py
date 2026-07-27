# -*- coding: utf-8 -*-
"""実ペア第3弾（キラトス案件）の回帰テスト。

診断A（音声バグ）: 参考のASR失敗(Fish残高不足)→台本(narration_text)が空になると、
  absent ガードが narration_mode の格納位置差で不発 → 空台本のフル音声合成が走り、
  壊れた音声が無音であるべき動画に混入していた。
  → (a) 空台本は TTS を丸ごと呼ばず必ず無音化（絶対無音化） (b) absent ガードは
     plan 直下 / project ルート直下の双方の narration_mode を読む。

診断B（商品不一致）: 商品1本ロックの参照画像が採用順先頭（=商品の写らない
  product_in_use のストック写真）になり、AI がボトルを捏造して「別物」になっていた。
  → product_solo（商品単体の物撮り）を優先して参照にする／複数あれば別角度も渡す。

実ffmpeg/実TTSは実行しない（run_ffmpeg・TTSバックエンドをmonkeypatch）。
"""
from pathlib import Path

import pytest

from pipeline import product_images
from pipeline import tts as tts_mod
from studio.server import jobs as jobs_mod
from studio.server import projects


# ---------------------------------------------------------------------------
# 診断A: ナレーション有無・残高エラー判定ヘルパ（純関数）
# ---------------------------------------------------------------------------

def test_is_narration_absent_mode_absent_always_true():
    assert tts_mod.is_narration_absent("absent", script_text="なにか喋る", shot_texts=["ナレ"]) is True


def test_is_narration_absent_script_only_empty_is_absent():
    # studio 再レンダ経路（shot_texts=None）: 台本が空/空白なら absent。
    assert tts_mod.is_narration_absent(None, script_text="") is True
    assert tts_mod.is_narration_absent(None, script_text="   \n ") is True
    assert tts_mod.is_narration_absent(None, script_text=None) is True


def test_is_narration_absent_script_only_nonempty_is_present():
    assert tts_mod.is_narration_absent(None, script_text="本編ナレーション") is False


def test_is_narration_absent_run_path_requires_all_shot_empty():
    # run.py 経路（shot_texts 指定）: 台本空 かつ 全 shot 空 のときだけ absent。
    assert tts_mod.is_narration_absent(None, script_text="", shot_texts=["", "  "]) is True
    # 1つでも shot にナレがあれば present。
    assert tts_mod.is_narration_absent(None, script_text="", shot_texts=["", "喋る"]) is False
    # shot_texts が空リストなら（run.py 旧条件 bool(shots) と同値で）present 扱い。
    assert tts_mod.is_narration_absent(None, script_text="", shot_texts=[]) is False


def test_is_balance_error():
    assert tts_mod.is_balance_error("http_error_402") is True
    assert tts_mod.is_balance_error("http_error_401") is False
    assert tts_mod.is_balance_error(None) is False


# ---------------------------------------------------------------------------
# 診断A: _render_project の絶対無音化（空台本・absent時に TTS を呼ばない）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path / "projects")
    projects.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    yield


def _make_project(theme, narration_text, narration_mode=None):
    project = projects.create_project(theme, 10.0, "mock", status="draft")
    clips_dir = projects.clips_dir(project["id"])
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for idx, (start, end) in enumerate([(0.0, 2.0), (0.0, 3.0)], start=1):
        sid = "s{}".format(idx)
        (clips_dir / "{}.mp4".format(sid)).write_bytes(b"\x00")
        shots.append({
            "id": sid, "order": idx - 1, "enabled": True, "prompt": "abstract",
            "caption": "テスト{}".format(idx),
            "clip_path": projects.media_relpath_for_clip(project["id"], "{}.mp4".format(sid)),
            "source_duration": end, "trim": {"start": start, "end": end},
        })
    project["plan"] = {
        "shots": shots, "narration_text": narration_text,
        "bgm": None, "sfx": [], "subtitle_style": dict(projects.DEFAULT_SUBTITLE_STYLE),
    }
    if narration_mode is not None:
        project["narration_mode"] = narration_mode
    projects.save_project(project)
    return project


def _capture_ffmpeg(monkeypatch):
    monkeypatch.setattr(jobs_mod.render, "run_ffmpeg",
                        lambda cmd, timeout_sec=None: {"returncode": 0, "stderr": ""})


def _spy_silent(monkeypatch):
    """synthesize_silent_track を、ファイルだけ書くスパイに差し替える（実ffmpeg回避）。"""
    calls = []

    def fake_silent(out_wav_path, duration_sec, cfg=None):
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        calls.append(float(duration_sec))
        return {"backend": "none", "duration_sec": float(duration_sec),
                "is_silent": True, "mode": "none"}

    monkeypatch.setattr(jobs_mod.tts_mod, "synthesize_silent_track", fake_silent)
    return calls


def _forbid_tts_backend(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("空台本/absent時に TTS バックエンドを呼んではいけない（絶対無音化）")
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend", boom)


class _FakeFullBackend:
    name = "fake_full"

    def synthesize(self, text, out_wav_path, cfg=None):
        assert text and text.strip(), "空台本でフル合成が呼ばれた（バグ）"
        Path(out_wav_path).write_bytes(b"RIFF....WAVEfmt ")
        return {"backend": "fake_full", "duration_sec": 5.0, "is_silent": False}


def test_render_project_empty_narration_text_forces_silence_and_no_tts(monkeypatch):
    """空台本(narration_text='')は TTS を呼ばず無音トラックにする（診断A・本丸）。"""
    project = _make_project("空台本無音化テスト", narration_text="")
    _capture_ffmpeg(monkeypatch)
    _forbid_tts_backend(monkeypatch)
    silent_calls = _spy_silent(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "none"
    assert tts_meta["is_silent"] is True
    assert len(silent_calls) == 1
    # 無音トラック尺は全ショットの表示尺合計（2.0+3.0）。
    assert silent_calls[0] == pytest.approx(5.0)


def test_render_project_absent_narration_mode_forces_silence(monkeypatch):
    """narration_text が非空でも project ルートの narration_mode='absent' なら無音（診断A・位置差）。"""
    project = _make_project("absentモード無音化テスト",
                            narration_text="本来は喋る台本", narration_mode="absent")
    _capture_ffmpeg(monkeypatch)
    _forbid_tts_backend(monkeypatch)
    silent_calls = _spy_silent(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "none"
    assert tts_meta["is_silent"] is True
    assert len(silent_calls) == 1


def test_render_project_absent_overrides_leftover_narration_segments(monkeypatch):
    """narration_segments が残っていても narration_mode='absent' なら sync を無効化し無音にする
    （codex-review P2: segment 経路が絶対無音化ガードを迂回しないこと）。"""
    project = _make_project("残留segments無音化テスト",
                            narration_text="", narration_mode="absent")
    # 陳腐化した narration_segments を残す（本来 absent なら無視されるべき）。
    project["narration_segments"] = {"s1": "残留した断片1", "s2": "残留した断片2"}
    projects.save_project(project)
    _capture_ffmpeg(monkeypatch)
    _forbid_tts_backend(monkeypatch)
    # segment 合成が呼ばれてはいけない。
    monkeypatch.setattr(jobs_mod.tts_mod, "synthesize_segments",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("absent時にsegment合成された")))
    silent_calls = _spy_silent(monkeypatch)
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "none"
    assert tts_meta["is_silent"] is True
    assert len(silent_calls) == 1


def test_render_project_nonempty_narration_text_still_synthesizes(monkeypatch):
    """非空の台本・absentでないなら従来どおりフル合成する（正常系を壊さない）。"""
    project = _make_project("正常系フル合成テスト", narration_text="ちゃんと喋る台本です。")
    _capture_ffmpeg(monkeypatch)
    monkeypatch.setattr(jobs_mod.tts_mod, "get_tts_backend",
                        lambda voice="Kyoko", cfg=None, **_kw: _FakeFullBackend())
    # synthesize_silent_track は呼ばれてはいけない。
    monkeypatch.setattr(jobs_mod.tts_mod, "synthesize_silent_track",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("非空台本で無音化された")))
    cfg = {"ffmpeg_bin": "/bin/ffmpeg", "voice": "Kyoko"}

    out_rel, out_duration, tts_meta = jobs_mod._render_project(project["id"], project["plan"], cfg)

    assert tts_meta["mode"] == "full"
    assert tts_meta["backend"] == "fake_full"


# ---------------------------------------------------------------------------
# 診断B: 商品1本ロックの参照画像を product_solo 優先で選ぶ
# ---------------------------------------------------------------------------

def _meta(entries):
    """entries: [(path, category, sharpness), ...] -> {path: {category,sharpness}}。"""
    return {p: {"category": c, "sharpness": s} for p, c, s in entries}


def test_select_product_refs_prefers_solo_over_in_use():
    """採用順先頭が in_use でも solo を先頭にする（診断Bの本丸）。"""
    paths = ["/p/004.jpg", "/p/005.jpg", "/p/006.jpg"]
    meta = _meta([
        ("/p/004.jpg", "product_in_use", "high"),
        ("/p/005.jpg", "product_solo", "high"),
        ("/p/006.jpg", "product_solo", "high"),
    ])
    refs = product_images._select_product_refs(paths, image_meta=meta)
    assert refs == ["/p/005.jpg", "/p/006.jpg"]
    # solo があるので in_use(004) はロック参照候補に含めない（混ぜると別商品捏造の原因）。
    assert "/p/004.jpg" not in refs


def test_select_product_refs_single_solo_excludes_in_use():
    """solo が1枚だけでも in_use を2枚目参照に混ぜない（codex-review P2）。"""
    paths = ["/p/004.jpg", "/p/005.jpg"]
    meta = _meta([
        ("/p/004.jpg", "product_in_use", "high"),
        ("/p/005.jpg", "product_solo", "high"),
    ])
    refs = product_images._select_product_refs(paths, image_meta=meta)
    assert refs == ["/p/005.jpg"]
    assert "/p/004.jpg" not in refs


def test_select_product_refs_high_sharpness_first():
    paths = ["/p/a.jpg", "/p/b.jpg"]
    meta = _meta([
        ("/p/a.jpg", "product_solo", "low"),
        ("/p/b.jpg", "product_solo", "high"),
    ])
    refs = product_images._select_product_refs(paths, image_meta=meta)
    assert refs[0] == "/p/b.jpg"  # high 鮮明を優先


def test_select_product_refs_in_use_only_fallback():
    paths = ["/p/x.jpg", "/p/y.jpg"]
    meta = _meta([
        ("/p/x.jpg", "product_in_use", "high"),
        ("/p/y.jpg", "product_in_use", "high"),
    ])
    refs = product_images._select_product_refs(paths, image_meta=meta)
    assert refs == paths  # solo が無ければ in_use をそのまま使う


def test_select_product_refs_no_meta_is_backward_compat():
    paths = ["/p/first.jpg", "/p/second.jpg"]
    assert product_images._select_product_refs(paths, image_meta=None) == paths


def _pshot(sid, desc_en):
    return {"id": sid, "visual_prompt": "a scene", "reference_visual": {"desc_en": desc_en}}


def _ref_spec(descs):
    return {"shots_ref": [{"start": i, "end": i + 1, "visual_desc_en": d} for i, d in enumerate(descs)]}


def test_assign_images_locks_to_solo_not_in_use_first():
    """実データ再現: paths先頭=in_use(004)、solo=005/006。全商品shotのロック参照が solo になる。"""
    shots = [
        _pshot("s1", "close-up of the toothpaste bottle"),
        _pshot("s2", "the product package on a table"),
    ]
    ref = _ref_spec(["close-up of the toothpaste bottle", "the product package on a table"])
    paths = ["/p/004.jpg", "/p/005.jpg", "/p/006.jpg"]
    meta = _meta([
        ("/p/004.jpg", "product_in_use", "high"),
        ("/p/005.jpg", "product_solo", "high"),
        ("/p/006.jpg", "product_solo", "high"),
    ])
    out = product_images.assign_images_to_shots(shots, paths, reference_spec=ref, image_meta=meta)
    for idx in (0, 1):
        refs = out[idx].get("reference_images") or []
        assert refs, out[idx]
        assert refs[0] == "/p/005.jpg", "ロック参照が solo でない: {}".format(refs)
        # in_use(004) は先頭ロック参照であってはならない。
        assert refs[0] != "/p/004.jpg"


def test_assign_images_passes_two_solo_refs_for_stability():
    """solo が2枚あれば先頭+2枚目までロック参照に渡す（別角度で認識安定・B3）。"""
    shots = [_pshot("s1", "the product bottle hero shot")]
    ref = _ref_spec(["the product bottle hero shot"])
    paths = ["/p/004.jpg", "/p/005.jpg", "/p/006.jpg"]
    meta = _meta([
        ("/p/004.jpg", "product_in_use", "high"),
        ("/p/005.jpg", "product_solo", "high"),
        ("/p/006.jpg", "product_solo", "high"),
    ])
    out = product_images.assign_images_to_shots(shots, paths, reference_spec=ref, image_meta=meta)
    refs = out[0].get("reference_images") or []
    assert refs[:2] == ["/p/005.jpg", "/p/006.jpg"]
    # ロック参照は最大2枚（in_use はここに混ぜない）。
    assert "/p/004.jpg" not in refs[:2]


def test_apply_product_lock_caps_lock_refs_at_two():
    shot = {"visual_prompt": "a scene"}
    product_images._apply_product_lock(shot, ["/p/a.jpg", "/p/b.jpg", "/p/c.jpg"])
    refs = shot["reference_images"]
    assert refs[:2] == ["/p/a.jpg", "/p/b.jpg"]
    assert "/p/c.jpg" not in refs  # 3枚目以降はロック参照に載せない


def test_apply_product_lock_accepts_str_backward_compat():
    shot = {"visual_prompt": "a scene"}
    product_images._apply_product_lock(shot, "/p/only.jpg")
    assert shot["reference_images"][0] == "/p/only.jpg"
    assert product_images._PRODUCT_LOCK_MARKER in shot["visual_prompt"].lower()
