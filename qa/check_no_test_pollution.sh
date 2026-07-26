#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# qa/check_no_test_pollution.sh
#
# pytest 全実行の前後で projects/ のディレクトリ数を比較し、テストが本物の projects/ を
# 汚染していないこと（増加0）を機械検証する。conftest.py の PROJECTS_ROOT 隔離 fixture が
# 効いていることの回帰ガード。CI／手動どちらでも使える。
#
# 使い方:
#   qa/check_no_test_pollution.sh              # pytest 全体を回して増加0を検証
#   qa/check_no_test_pollution.sh -m "not slow"  # 追加の pytest 引数はそのまま透過
#
# 終了コード: 0=汚染なし(増加0)  1=汚染検出(増加>0)  2=pytest自体が失敗
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECTS_DIR="${REPO_ROOT}/projects"

cd "${REPO_ROOT}"

count_projects() {
  # projects/ 直下のディレクトリ数（隠しディレクトリ含む・ファイルは除外）
  find "${PROJECTS_DIR}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '
}

before="$(count_projects)"
echo "[check_no_test_pollution] before: ${before} project dirs"

# .venv があれば使う（無ければ環境の python/pytest をそのまま使う）
PYTEST="pytest"
if [ -x "${REPO_ROOT}/.venv/bin/pytest" ]; then
  PYTEST="${REPO_ROOT}/.venv/bin/pytest"
fi

set +e
"${PYTEST}" -q "$@"
pytest_rc=$?
set -e

after="$(count_projects)"
echo "[check_no_test_pollution] after:  ${after} project dirs"

delta=$(( after - before ))
if [ "${delta}" -ne 0 ]; then
  echo "[check_no_test_pollution] FAIL: projects/ が ${delta} 件増えました（テスト隔離漏れ＝汚染）。"
  echo "  → 増えた分を特定し、conftest.py の PROJECTS_ROOT 隔離 fixture を迂回しているテストを修正してください。"
  exit 1
fi

echo "[check_no_test_pollution] OK: projects/ 増加0（テストは本物の projects/ を汚染していません）。"

if [ "${pytest_rc}" -ne 0 ]; then
  echo "[check_no_test_pollution] NOTE: 汚染は0でしたが pytest 自体は失敗しています (rc=${pytest_rc})。"
  exit 2
fi

exit 0
