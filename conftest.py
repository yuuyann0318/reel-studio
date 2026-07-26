# -*- coding: utf-8 -*-
"""ルート conftest: 全テストで PROJECTS_ROOT を tmp_path へ隔離する（恒久・autouse）。

背景（再発した構造バグ）:
  studio/server/projects.py の PROJECTS_ROOT は既定で project_root()/"projects"（＝本物の
  Studio 一覧が並ぶディレクトリ）を指す。create_project() / POST /api/projects 等を叩くテストが
  PROJECTS_ROOT を monkeypatch せずに走ると、本物の projects/ に「単体テスト:…」等のプロジェクトを
  作成し、generating のまま残してユーザーの一覧を汚染していた（例: test_bgm_mode_none.py・
  test_studio_api.py の一部）。個別テストの隔離漏れに依存せず、ここで全テストを一律に隔離する。

仕組み:
  projects モジュールの全ファイル書き込み（save_project / create_project / clips_dir /
  renders_dir など）は module グローバル PROJECTS_ROOT を呼び出し時に都度参照する。よって
  monkeypatch.setattr で PROJECTS_ROOT を tmp_path 配下へ差し替えれば、app.py の POST /api/projects
  経由・jobs.py 経由・projects 直接呼び出しの全経路が tmp_path に閉じ込められる。
  既に module 内で同種の autouse fixture を持つテスト（PROJECTS_ROOT を tmp_path に差し替え済み）とは
  冪等に共存する（このルート fixture が先に走り、モジュール側が同じ tmp_path で上書きするだけ）。

注意: PROJECTS_ROOT の実パスそのものを検証するテストは存在しないことを確認済み（grep）。
"""
from __future__ import annotations

import sys
import time

import pytest

from studio.server import projects as _projects_mod


def _drain_shared_job_manager(timeout_sec=90.0):
    """共有 JobManager（studio.server.jobs.job_manager の singleton・ワーカースレッド1本）に
    投入済みで未完了のジョブが残っていれば、完了するまで（最大 timeout_sec）待つ。

    なぜ必要か（teardown 競合による projects/ 汚染）:
      premiere-export / generate 等のエンドポイントテストは POST で「実」ワーカースレッドに
      ジョブを投入し、SSE 完走を待たずに 202 だけ検証して return するものがある。すると
      直後の fixture teardown で PROJECTS_ROOT が本物へ復元され、ワーカースレッドは
      get_project(=tmp で成功) の後に project_dir(=復元後の本物 PROJECTS_ROOT) 側へ
      premiere/ 一式を書き込み、project.json 無しの残骸が本物の projects/ に生まれる。
      隔離を解除する前にジョブを必ず完了させることで、この競合を根絶する。

    _queue は queue.Queue で worker が task_done() を呼ぶため unfinished_tasks が信頼できる。

    タイムアウトの扱い（codex-review P1）: 万一 timeout_sec 内に排出しきれない場合、黙って
    return すると隔離が解除された後もワーカーが本物の PROJECTS_ROOT へ書き込み続け、防ごうと
    した汚染そのものが起きる。よって沈黙して続行せず RuntimeError を送出し、「隔離が解除される
    前にジョブが終わらなかった」ことを可視の失敗として顕在化させる（mock backend のジョブは
    通常数秒で終わるため、この分岐に入るのは実質的にハング＝要調査のシグナル）。
    """
    jobs_mod = sys.modules.get("studio.server.jobs")
    if jobs_mod is None:
        return
    manager = getattr(jobs_mod, "job_manager", None)
    if manager is None:
        return
    q = getattr(manager, "_queue", None)
    if q is None:
        return
    deadline = time.time() + timeout_sec
    while getattr(q, "unfinished_tasks", 0) > 0:
        if time.time() >= deadline:
            raise RuntimeError(
                "共有 job_manager のジョブが {:.0f}s 以内に完了しませんでした "
                "(unfinished_tasks={})。PROJECTS_ROOT の隔離解除前にジョブを完了できないと "
                "本物の projects/ を汚染するため、teardown を失敗させます。".format(
                    timeout_sec, getattr(q, "unfinished_tasks", "?")
                )
            )
        time.sleep(0.02)


def _repoint_media_projects_mount(monkeypatch, isolated_dir):
    """studio.server.app が既にロード済みなら、/media/projects の StaticFiles マウントを
    隔離先へ張り替える（未ロードなら何もしない）。

    app.py は import 時に StaticFiles(directory=str(projects.PROJECTS_ROOT)) をマウントし、
    ディレクトリ文字列を確定してしまう。PROJECTS_ROOT だけ tmp_path に差し替えても、この
    マウントは本物の projects/ を配信し続けるため、隔離下で生成したクリップ/レンダーを
    /media/projects 経由で GET すると 404 になる（test_generate_edit_render_e2e）。
    StaticFiles のパス探索は all_directories を走査するため、directory と all_directories の
    両方を隔離先へ向ける。monkeypatch 経由なのでテスト終了時に自動で復元される。
    """
    app_mod = sys.modules.get("studio.server.app")
    if app_mod is None:
        return
    app = getattr(app_mod, "app", None)
    if app is None:
        return
    target = str(isolated_dir)
    for route in getattr(app, "routes", []):
        if getattr(route, "name", None) != "media_projects":
            continue
        static = getattr(route, "app", None)
        if static is None:
            continue
        if hasattr(static, "directory"):
            monkeypatch.setattr(static, "directory", target, raising=False)
        if hasattr(static, "all_directories"):
            monkeypatch.setattr(static, "all_directories", [target], raising=False)


@pytest.fixture(autouse=True)
def _isolate_projects_root_globally(tmp_path, monkeypatch):
    """全テスト共通: projects.PROJECTS_ROOT を tmp_path/projects へ隔離する。

    これにより本物の projects/ ディレクトリにテスト用プロジェクトが作られなくなる。
    app が読み込み済みの場合は /media/projects 配信マウントも同じ隔離先へ張り替える。
    """
    isolated = tmp_path / "projects"
    monkeypatch.setattr(_projects_mod, "PROJECTS_ROOT", isolated)
    isolated.mkdir(parents=True, exist_ok=True)
    _repoint_media_projects_mount(monkeypatch, isolated)
    yield
    # 隔離を解除（monkeypatch 復元）する前に、共有ワーカーの在庫ジョブを完了させる。
    # これをしないと非同期ジョブが本物の PROJECTS_ROOT へ書き戻して汚染する（上記 docstring 参照）。
    _drain_shared_job_manager()
