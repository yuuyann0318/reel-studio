#!/bin/bash
# Reel Studio バックエンドサーバ起動スクリプト。
# 127.0.0.1:8787 固定（ポートは変更しないこと。フロント側(studio/web/)と合わせているため）。
#
# 使い方:
#   "./studio/start_server.sh"
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"

cd "${REPO_ROOT}"
exec "${REPO_ROOT}/.venv/bin/python3" -m uvicorn studio.server.app:app \
  --host 127.0.0.1 \
  --port 8787
