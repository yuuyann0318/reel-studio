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

# 起動前ヘルスチェック（AI=claude 実疎通 / ffmpeg / ffprobe / yt-dlp の3点+）を標準出力へ。
# NG でも起動は続行する（UI 側の赤バナーでも通知するため）。claude 疎通は極小の課金が発生する。
echo "--- Reel Studio 起動前チェック ---"
"${REPO_ROOT}/.venv/bin/python3" - <<'PYCHECK' || echo "（ヘルスチェックの実行に失敗しました。サーバ起動は続行します）"
import sys
sys.path.insert(0, ".")
from studio.server.app import compute_health
res = compute_health(check_claude=True)
label = {"claude": "AI(claude)", "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "yt_dlp": "yt-dlp"}
for k, v in res["checks"].items():
    mark = "OK " if v.get("ok") else "NG "
    print("  [{}] {}: {}".format(mark, label.get(k, k), (v.get("detail") or "")[:80]))
print("  => 総合: {}".format("OK（すべて疎通）" if res["ok"] else "NG（上のNG項目を確認してください）"))
PYCHECK
echo "---------------------------------"

exec "${REPO_ROOT}/.venv/bin/python3" -m uvicorn studio.server.app:app \
  --host 127.0.0.1 \
  --port 8787
