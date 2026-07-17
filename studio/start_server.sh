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

# Fish Audio APIキー: ~/.claude/secrets/fish_audio_key があれば自動で環境変数へ載せる
# （キーはgit管理外・ログにも出さない。無ければ say フォールバックで動く）
FISH_KEY_FILE="${HOME}/.claude/secrets/fish_audio_key"
if [ -s "${FISH_KEY_FILE}" ]; then
  FISH_AUDIO_API_KEY="$(cat "${FISH_KEY_FILE}")"
  export FISH_AUDIO_API_KEY
fi

exec "${REPO_ROOT}/.venv/bin/python3" -m uvicorn studio.server.app:app \
  --host 127.0.0.1 \
  --port 8787
