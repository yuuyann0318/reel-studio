# -*- coding: utf-8 -*-
"""Premiere Pro自動化(Phase B)のセットアップ状態チェック。

check_setup() は、pymiere経由でPremiereへ自動インポートできる状態かどうかを
3つの観点で確認する:
  (a) pymiereパッケージがimportできるか（未インストールなら不可）
  (b) Pymiere Link（CEP拡張）がHTTP経由で応答するか
      （pymiereのExtendScript疎通の軽い事前チェック。localhost:3000系・タイムアウト2秒。
       例外・タイムアウトはすべて "pymiere_link_unreachable" として扱う）
  (c) Adobe Premiere Proのプロセスが実際に起動しているか（`pgrep -f "Adobe Premiere Pro"`）

3つとも揃わない限り自動インポートは実行せず、studio/server/jobs.py側で
Phase A（パッケージ+README）へ静かにフォールバックする。

各判定はテストから注入できるようcallable引数として差し替え可能にしてある
（本番では引数省略時に実際のチェックを行う）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import subprocess

# Pymiere Link（CEP拡張）が待受けるローカルサーバのURL。
PYMIERE_LINK_URL = "http://localhost:3000"
PYMIERE_LINK_TIMEOUT_SEC = 2

# `pgrep -f` に渡すプロセス名パターン（Adobe Premiere Proのプロセス名想定）。
PREMIERE_PROCESS_PATTERN = "Adobe Premiere Pro"

MISSING_PYMIERE_NOT_INSTALLED = "pymiere_not_installed"
MISSING_PYMIERE_LINK_UNREACHABLE = "pymiere_link_unreachable"
MISSING_PREMIERE_NOT_RUNNING = "premiere_not_running"


def _default_check_pymiere_importable():
    """pymiereパッケージが実際にimportできるかどうか。"""
    try:
        import pymiere  # noqa: F401
    except Exception:
        return False
    return True


def _default_check_pymiere_link_reachable():
    """Pymiere Link（CEP拡張のローカルHTTPサーバ）に軽く疎通確認する。

    タイムアウト・接続拒否・その他すべての例外を「応答なし」として扱う
    （このチェックはあくまで事前の軽いヘルスチェックであり、実際のExtendScript
    呼び出し可否を厳密に保証するものではない）。
    """
    try:
        import urllib.request

        req = urllib.request.Request(PYMIERE_LINK_URL)
        urllib.request.urlopen(req, timeout=PYMIERE_LINK_TIMEOUT_SEC)
        return True
    except Exception:
        return False


def _default_check_premiere_running():
    """Adobe Premiere Proのプロセスが起動中かどうかを `pgrep -f` で確認する。"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", PREMIERE_PROCESS_PATTERN],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_setup(
    check_pymiere_importable=None,
    check_pymiere_link_reachable=None,
    check_premiere_running=None,
):
    """Premiere自動インポートに必要な3条件を確認する。

    Args:
        check_pymiere_importable: callable() -> bool | None。テスト注入用（省略時は実チェック）。
        check_pymiere_link_reachable: callable() -> bool | None。テスト注入用。
        check_premiere_running: callable() -> bool | None。テスト注入用。

    Returns:
        dict: {"ready": bool, "missing": [str, ...]}
              missing の要素は以下のいずれか（判定失敗ごとに追加。順不同ではなく
              チェック順=pymiere/link/processの順で追加される）:
                - "pymiere_not_installed"
                - "pymiere_link_unreachable"
                - "premiere_not_running"
              いずれかの注入callableが例外を送出した場合も、そのチェックは
              「失敗（=missingに追加）」として扱う（安全側に倒す）。
    """
    check_pymiere_importable = check_pymiere_importable or _default_check_pymiere_importable
    check_pymiere_link_reachable = check_pymiere_link_reachable or _default_check_pymiere_link_reachable
    check_premiere_running = check_premiere_running or _default_check_premiere_running

    missing = []

    try:
        pymiere_ok = bool(check_pymiere_importable())
    except Exception:
        pymiere_ok = False
    if not pymiere_ok:
        missing.append(MISSING_PYMIERE_NOT_INSTALLED)

    try:
        link_ok = bool(check_pymiere_link_reachable())
    except Exception:
        link_ok = False
    if not link_ok:
        missing.append(MISSING_PYMIERE_LINK_UNREACHABLE)

    try:
        running_ok = bool(check_premiere_running())
    except Exception:
        running_ok = False
    if not running_ok:
        missing.append(MISSING_PREMIERE_NOT_RUNNING)

    return {"ready": len(missing) == 0, "missing": missing}
