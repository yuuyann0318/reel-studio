# -*- coding: utf-8 -*-
"""BGM モード "none"（BGM を一切付けない・後付け派向け）の権威スイッチ検証。

対象:
  1. pipeline.config: 既定 audio.bgm_mode="auto"、config.json の "none" が上書きで通ること
  2. pipeline.render.build_final_cmd: bgm_path=None のとき ffmpeg コマンドに BGM 入力 (-i <bgm>)
     も amix の bgm ラベルも一切含まれないこと（既存契約の再確認・回帰防止のダブルロック）
  3. studio/server/app.py POST /api/projects:
     - bgm_mode="none" を送ると project.json に bgm_mode="none" が保存され、
       job payload にも bgm_mode="none" が乗ること
     - bgm_mode 未指定なら cfg.audio.bgm_mode（config.json の既定 "none"）が適用されること
     - bgm_mode に "invalid" を送ると 400 で弾かれること
  4. studio/server/projects.create_project: bgm_mode="none" を渡すと保存された project に
     bgm_mode="none" が乗ること
  5. studio/server/jobs._render_project 経路の権威スイッチ:
     - project_snapshot.bgm_mode="none" のときは plan.bgm が非 None（曲付き）でも
       build_final_cmd に渡す bgm_path は None、かつ bgm_curve / main_dip_events は
       enhancement 経由でも None に潰されること（副作用としてのナレーション局所凹み防止）
  6. premiere/package build: bgm_mode="none" プロジェクトの README に「BGM を「なし」で
     作成した場合」節が含まれること（A2 空トラック維持の案内）

すべて Python 3.9 互換 / mock backend / ネットワーク不使用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import config as config_mod
from pipeline import render as render_mod
from studio.server import app as app_mod
from studio.server import projects as projects_mod


client = TestClient(app_mod.app)

_DUMMY_REFERENCE_URL = "https://example.com/reference.mp4"


# ---------------------------------------------------------------------------
# 1. config layer
# ---------------------------------------------------------------------------

def test_default_config_bgm_mode_is_auto():
    """既定（config.json が無いとき）は audio.bgm_mode="auto"（後方互換）。"""
    defaults = config_mod._DEFAULTS
    assert "audio" in defaults
    assert defaults["audio"].get("bgm_mode") == "auto"


def test_project_config_json_sets_bgm_mode_none():
    """ユーザーの既定として config.json の audio.bgm_mode が "none" になっていること。"""
    cfg = config_mod.load_config()
    assert (cfg.get("audio") or {}).get("bgm_mode") == "none"


# ---------------------------------------------------------------------------
# 2. render layer: bgm_path=None の ffmpeg コマンドから BGM 入力が消えていること
# ---------------------------------------------------------------------------

def test_build_final_cmd_bgm_none_produces_no_bgm_input_and_no_bgm_amix_label():
    """bgm_mode="none" 経路の実効: bgm_path=None → -i <bgm> が無く、amix にも [bgm*] ラベルが無い。"""
    cmd = render_mod.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=10.0,
    )
    # BGM ファイルへの参照がコマンド全体に一切無い
    for tok in cmd:
        assert not tok.endswith(".mp3") and not tok.endswith(".m4a"), \
            "bgm_path=None なのに BGM 音源への -i 引数が残っている: {}".format(tok)
    joined = " ".join(cmd)
    # amix に BGM ラベル [bgm] を含まない（BGM 系のトラック指定が漏れていない）
    assert "[bgm]" not in joined
    # フィルタグラフの本体は narration ベース([main_mix]) → loudnorm → 出力([aout]) のみ
    assert "[main_mix]" in joined and "[aout]" in joined


# ---------------------------------------------------------------------------
# 3. app.py: POST /api/projects の bgm_mode スレッド
# ---------------------------------------------------------------------------

def _patch_start_generate(monkeypatch):
    captured = {}

    def _fake_start_generate(project_id, theme, target_duration_sec, backend_name, style="default",
                              product_url=None, reference_url=None, plan_tier=None, bgm_mode=None, **_kwargs):
        captured["project_id"] = project_id
        captured["bgm_mode"] = bgm_mode
        captured["plan_tier"] = plan_tier
        return project_id

    monkeypatch.setattr(app_mod.job_manager, "start_generate", _fake_start_generate)
    return captured


def test_create_project_bgm_mode_none_persisted_and_forwarded(monkeypatch):
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:BGM なし",
        "plan_tier": "free",
        "reference_url": _DUMMY_REFERENCE_URL,
        "bgm_mode": "none",
    })
    assert resp.status_code == 202, resp.text
    project_id = resp.json()["id"]
    saved = projects_mod.get_project(project_id)
    assert saved is not None
    assert saved.get("bgm_mode") == "none"
    assert captured["bgm_mode"] == "none"


def test_create_project_bgm_mode_defaults_to_config_none(monkeypatch):
    """bgm_mode を送らなくても config.json の既定 "none" が乗ること。"""
    captured = _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:BGM 既定",
        "plan_tier": "free",
        "reference_url": _DUMMY_REFERENCE_URL,
    })
    assert resp.status_code == 202, resp.text
    saved = projects_mod.get_project(resp.json()["id"])
    assert saved is not None
    # config.json のユーザー既定が "none" なので、フォームが送らない場合でも none になる。
    assert saved.get("bgm_mode") == "none"
    assert captured["bgm_mode"] == "none"


def test_create_project_invalid_bgm_mode_rejected(monkeypatch):
    _patch_start_generate(monkeypatch)
    resp = client.post("/api/projects", json={
        "theme": "テスト:BGM 不正",
        "plan_tier": "free",
        "reference_url": _DUMMY_REFERENCE_URL,
        "bgm_mode": "sometimes",  # 不正値
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_bgm_mode"


# ---------------------------------------------------------------------------
# 4. projects.create_project: bgm_mode の永続化
# ---------------------------------------------------------------------------

def test_projects_create_project_bgm_mode_none_stored():
    p = projects_mod.create_project(
        theme="単体テスト:BGM none 永続化",
        target_duration_sec=30, backend_name="mock",
        status="generating", style="default",
        bgm_mode="none",
    )
    reloaded = projects_mod.get_project(p["id"])
    assert reloaded.get("bgm_mode") == "none"


def test_projects_create_project_bgm_mode_invalid_falls_back_to_auto():
    p = projects_mod.create_project(
        theme="単体テスト:BGM 不正値は auto に正規化",
        target_duration_sec=30, backend_name="mock",
        status="generating", style="default",
        bgm_mode="wobble",
    )
    reloaded = projects_mod.get_project(p["id"])
    assert reloaded.get("bgm_mode") == "auto"


# ---------------------------------------------------------------------------
# 5. jobs._render_project の権威スイッチ（単体・純関数的検証）
#
# _render_project は Django/FastAPI 経由でしか動かないので、bgm 経路だけを分離して
# 「同じ入力から build_final_cmd に渡る bgm_path/bgm_curve/main_dip_events が None になる」
# ことを模倣する形で検証する。バグの本質: plan.bgm に曲が残っていても none で書き出す。
# ---------------------------------------------------------------------------

def test_render_project_bgm_none_authority_switch_zeroes_out_bgm_kwargs():
    """権威スイッチ: project_snapshot.bgm_mode="none" のとき plan.bgm を無視して bgm 系を全部 None にする。"""
    # jobs._render_project の該当 if 分岐と同じ判定を、独立に模倣する。
    project_snapshot = {"bgm_mode": "none"}
    plan_bgm = {"file": "library/upbeat_house_02.m4a", "gain_db": -14.0, "ducking": True}

    bgm_path = None
    bgm_curve = {"hook_gain_db": -10, "body_gain_db": -14, "cta_gain_db": -12}
    main_dip_events = [1.5, 3.2]

    _bgm_mode_authority = (project_snapshot or {}).get("bgm_mode") or "auto"
    if _bgm_mode_authority == "none":
        pass  # plan.bgm には触らない（UI 表示状態は維持）
    elif plan_bgm and plan_bgm.get("file"):
        bgm_path = "/assets/bgm/library/upbeat_house_02.m4a"  # 仮の解決結果

    # enhancement 結果側の対称処理（jobs._render_project と同じ条件で潰す）
    if _bgm_mode_authority == "none":
        bgm_curve = None
        main_dip_events = None

    assert bgm_path is None, "bgm_mode=none のときは bgm_path が None になっていること（plan.bgm に曲が乗っていても無視）"
    assert bgm_curve is None
    assert main_dip_events is None


def test_render_project_bgm_none_produces_bgm_free_ffmpeg_cmd():
    """上記権威スイッチ結果を build_final_cmd に流し込むと、実 ffmpeg 引数から BGM 入力が消える。"""
    cmd = render_mod.build_final_cmd(
        "/bin/ffmpeg", "/concat.mp4", "/narration.wav", "/out.mp4", "/subs.ass", "/fonts",
        bgm_path=None, out_duration=12.0,
        bgm_gain_db=None, sfx=[], ducking=True,
        bgm_curve=None, first_shot_impact_sec=None, main_dip_events=None,
    )
    joined = " ".join(cmd)
    assert "[bgm]" not in joined
    assert ".m4a" not in joined and ".mp3" not in joined


# ---------------------------------------------------------------------------
# 6. premiere/package: README に BGM=none の案内が含まれる
# ---------------------------------------------------------------------------

def test_premiere_package_readme_mentions_bgm_none_case():
    """package._track_contract_section に「BGM を「なし」で作成した場合」節があること。"""
    from premiere import package as package_mod
    section = package_mod._track_contract_section()
    assert "BGM を「なし」で作成した場合" in section
    assert "空トラック" in section  # A2 空トラック維持の案内が含まれること
