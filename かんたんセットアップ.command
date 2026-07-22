#!/bin/bash
# ============================================================
#  higgsfield-auto-reel かんたんセットアップ
#  ダブルクリックで動く冪等ウィザード。
#  読者が自分でやるのは「アカウント登録・キー貼り付け・ログインOK」の3つだけ。
#  何度実行しても安全（済んだ段はスキップします）。
# ============================================================
set -uo pipefail

# --- OS判定: macOS 専用 ---
OS_KERNEL="$(uname -s 2>/dev/null || echo unknown)"
if [ "${OS_KERNEL}" != "Darwin" ]; then
  echo "============================================================"
  echo "  このセットアップは macOS 専用です"
  echo "============================================================"
  echo "  現在のOS: ${OS_KERNEL}"
  echo
  echo "  Reel Studio は macOS (Apple Silicon / Intel Mac) でのみ動作します。"
  echo "  Windows / Linux では現時点でサポートしていません。"
  echo
  echo "  Mac をお持ちであれば、その Mac 上でこのファイルを"
  echo "  もう一度ダブルクリックしてください。"
  echo "============================================================"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
LOG_FILE="/tmp/reel_setup.log"

# --- 表示ヘルパ ---
if [ -t 1 ]; then
  GREEN=$(tput setaf 2 2>/dev/null || echo)
  YELLOW=$(tput setaf 3 2>/dev/null || echo)
  BLUE=$(tput setaf 4 2>/dev/null || echo)
  RED=$(tput setaf 1 2>/dev/null || echo)
  BOLD=$(tput bold 2>/dev/null || echo)
  RESET=$(tput sgr0 2>/dev/null || echo)
else
  GREEN=""; YELLOW=""; BLUE=""; RED=""; BOLD=""; RESET=""
fi

log()  { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }
ok()   { echo "${GREEN}✅ $*${RESET}"; log "[OK] $*"; }
run()  { echo "${BLUE}▶  $*${RESET}"; log "[RUN] $*"; }
skip() { echo "${YELLOW}⏭  $*${RESET}"; log "[SKIP] $*"; }
warn() { echo "${YELLOW}⚠  $*${RESET}"; log "[WARN] $*"; }
err()  { echo "${RED}✗ $*${RESET}"; log "[ERR] $*"; }
info() { echo "   $*"; }
header(){ echo; echo "${BOLD}=== $* ===${RESET}"; log "=== $* ==="; }

REPORT=()
add_report(){ REPORT+=("$1"); }

trap 'echo; err "中断されました。もう一度ダブルクリックで続きから再開できます。"; exit 130' INT

# --- ログ初期化 ---
: > "$LOG_FILE"
log "setup start at ${REPO_ROOT}"

clear || true
cat <<'BANNER'
============================================================
  higgsfield-auto-reel かんたんセットアップ
  面倒な準備は代行します。あなたがやるのは3つだけ:
    1) 途中で開くページで「アカウント登録」する
    2) 発行されたキーを画面に「貼り付け」する
    3) 出てきたブラウザで「ログインOK」を押す
============================================================
BANNER
info "作業フォルダ: ${REPO_ROOT}"
info "詳細ログ   : ${LOG_FILE}"
echo

# ============================================================
# 1) 環境チェック
# ============================================================
header "1/9 環境チェック"
OS_VER="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
ARCH="$(uname -m)"
info "macOS ${OS_VER} / ${ARCH}"

if xcode-select -p >/dev/null 2>&1; then
  ok "Xcode Command Line Tools（開発道具）は導入済み"
else
  warn "Xcode Command Line Tools が未導入です。案内ダイアログを開きます..."
  xcode-select --install 2>/dev/null || true
  echo
  info "画面のダイアログで「インストール」を押し、完了後 Enter を押してください。"
  read -r _ || true
fi
add_report "✅ 環境チェック"

# ============================================================
# 2) Python 環境と依存ライブラリ
# ============================================================
header "2/9 Python環境の準備"
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 が見つかりません。ターミナルで xcode-select --install を実行してください。"
  exit 1
fi
info "$(python3 --version 2>&1)"

VENV="${REPO_ROOT}/.venv"
if [ -x "${VENV}/bin/python3" ]; then
  ok "作業用の部屋(.venv)は既にあります"
else
  run ".venv を作成中..."
  if python3 -m venv "${VENV}" >>"$LOG_FILE" 2>&1; then
    ok ".venv 作成完了"
  else
    err ".venv の作成に失敗。ログ末尾を確認してください: ${LOG_FILE}"
    exit 1
  fi
fi

if "${VENV}/bin/python3" -c "import fastapi, uvicorn, httpx, yt_dlp, curl_cffi" >/dev/null 2>&1; then
  ok "必要なライブラリは既にインストール済み"
else
  run "pip install -r requirements.txt（1〜3分）..."
  "${VENV}/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1 || true
  if "${VENV}/bin/pip" install -r "${REPO_ROOT}/requirements.txt" >>"$LOG_FILE" 2>&1; then
    ok "ライブラリのインストール完了"
  else
    err "pip install に失敗。対処: ネット環境を確認し再実行 / brew install openssl も検討"
    tail -5 "$LOG_FILE"
    exit 1
  fi
fi
add_report "✅ Python環境"

# ============================================================
# 3) ffmpeg / ffprobe
# ============================================================
header "3/9 ffmpeg / ffprobe を用意"
BIN_DIR="${REPO_ROOT}/bin"
mkdir -p "$BIN_DIR"

download_ff() {
  local name="$1"
  local url="https://evermeet.cx/ffmpeg/getrelease/${name}/zip"
  local tmpzip="/tmp/reel_${name}.zip"
  run "$name を evermeet.cx からダウンロード..."
  if curl -fL --max-time 120 -o "$tmpzip" "$url" 2>>"$LOG_FILE"; then
    if unzip -o "$tmpzip" -d "$BIN_DIR" >>"$LOG_FILE" 2>&1; then
      chmod +x "${BIN_DIR}/${name}" 2>/dev/null || true
      xattr -d com.apple.quarantine "${BIN_DIR}/${name}" 2>/dev/null || true
      if "${BIN_DIR}/${name}" -version >/dev/null 2>&1; then
        ok "$name 導入完了"
        rm -f "$tmpzip"
        return 0
      fi
    fi
  fi
  rm -f "$tmpzip"
  warn "$name の自動ダウンロード失敗。Homebrewでの代替: brew install ffmpeg"
  return 1
}

for tool in ffmpeg ffprobe; do
  if [ -x "${BIN_DIR}/${tool}" ] && "${BIN_DIR}/${tool}" -version >/dev/null 2>&1; then
    ok "bin/${tool} は動作OK"
  else
    download_ff "$tool" || true
  fi
done
add_report "✅ ffmpeg / ffprobe"

# ============================================================
# 4) yt-dlp
# ============================================================
header "4/9 yt-dlp を用意"
YTDLP="${BIN_DIR}/yt-dlp_macos"
if [ -x "$YTDLP" ] && "$YTDLP" --version >/dev/null 2>&1; then
  ok "bin/yt-dlp_macos は動作OK ($("$YTDLP" --version))"
else
  run "yt-dlp を GitHub releases から取得..."
  YT_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
  if curl -fL --max-time 120 -o "$YTDLP" "$YT_URL" 2>>"$LOG_FILE"; then
    chmod +x "$YTDLP" 2>/dev/null || true
    xattr -d com.apple.quarantine "$YTDLP" 2>/dev/null || true
    if "$YTDLP" --version >/dev/null 2>&1; then
      ok "yt-dlp 導入完了 ($("$YTDLP" --version))"
    else
      warn "yt-dlp をダウンロードしたが起動確認できず。手動: brew install yt-dlp"
    fi
  else
    warn "yt-dlp のダウンロード失敗（後で再実行するか brew install yt-dlp）"
  fi
fi
add_report "✅ yt-dlp"

# ============================================================
# 5) SFX / BGM ライブラリ
# ============================================================
header "5/9 効果音 / BGM ライブラリを生成"
SFX_GEN_COUNT=$(ls "${REPO_ROOT}/assets/sfx/" 2>/dev/null | grep -c "_gen_" || true)
SFX_GEN_COUNT=${SFX_GEN_COUNT:-0}
BGM_COUNT=$(ls "${REPO_ROOT}/assets/bgm/library/" 2>/dev/null | wc -l | tr -d ' ')
BGM_COUNT=${BGM_COUNT:-0}

if [ "$SFX_GEN_COUNT" -ge 10 ]; then
  ok "SFXライブラリは既に生成済み（${SFX_GEN_COUNT}個）"
else
  run "SFX を合成（外部通信なし・1〜2分）..."
  if "${VENV}/bin/python3" "${REPO_ROOT}/assets/sfx/generate_sfx_library.py" >>"$LOG_FILE" 2>&1; then
    ok "SFXライブラリ生成完了"
  else
    warn "SFX生成に失敗。省略して続行します（動画作成には支障なし）。"
  fi
fi
if [ "$BGM_COUNT" -ge 5 ]; then
  ok "BGMライブラリは既に生成済み（${BGM_COUNT}曲）"
else
  run "BGM を合成（外部通信なし・1〜2分）..."
  if "${VENV}/bin/python3" "${REPO_ROOT}/assets/bgm/generate_starter_pack.py" >>"$LOG_FILE" 2>&1; then
    ok "BGMライブラリ生成完了"
  else
    warn "BGM生成に失敗。省略して続行します（動画作成には支障なし）。"
  fi
fi
add_report "✅ SFX / BGM"

# ============================================================
# 6) Claude Code
# ============================================================
header "6/9 Claude Code の確認"
CLAUDE_BIN=""
for cand in "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null || true)"; do
  if [ -n "${cand:-}" ] && [ -x "${cand}" ]; then CLAUDE_BIN="$cand"; break; fi
done

if [ -n "$CLAUDE_BIN" ]; then
  ok "Claude Code は導入済み ($CLAUDE_BIN)"
else
  echo
  info "Claude Code が入っていません。公式インストーラを実行しますか?"
  printf "   Enter=はい / n=あとで手動 : "
  read -r ans || ans=""
  if [ "${ans}" != "n" ] && [ "${ans}" != "N" ]; then
    run "curl -fsSL https://claude.ai/install.sh | bash ..."
    if curl -fsSL https://claude.ai/install.sh 2>>"$LOG_FILE" | bash >>"$LOG_FILE" 2>&1; then
      export PATH="$HOME/.local/bin:$PATH"
      if [ -x "$HOME/.local/bin/claude" ]; then
        CLAUDE_BIN="$HOME/.local/bin/claude"
        ok "Claude Code 導入完了"
      fi
    fi
    if [ -z "$CLAUDE_BIN" ]; then
      warn "自動インストールに失敗。手動: https://code.claude.com/docs/en/quickstart"
    fi
  else
    skip "Claude Code はあとで導入（練習モードでは無くても動きます）"
    add_report "⏭  Claude Code（あとで導入）"
  fi
fi

if [ -n "$CLAUDE_BIN" ]; then
  info "Claude Code の応答確認（最大15秒・ログイン未済でも致命的ではありません）..."
  # /usr/bin/timeout は macOS 標準に無いため perl で代替
  if perl -e 'alarm shift; exec @ARGV' 15 "$CLAUDE_BIN" -p "1+1=?" >/dev/null 2>&1; then
    ok "Claude Code が正しく応答しました"
    add_report "✅ Claude Code"
  else
    warn "Claude Code は導入済みですが応答しませんでした。ログインが必要かも。"
    info "後で ターミナルで  ${CLAUDE_BIN}  を起動し、指示に従ってログインしてください。"
    add_report "⚠  Claude Code（要ログイン）"
  fi
fi

# ============================================================
# 7) Fish Audio APIキー（任意）
# ============================================================
header "7/9 Fish Audio APIキー（任意・スキップOK）"
SECRETS_DIR="$HOME/.claude/secrets"
FISH_KEY_FILE="${SECRETS_DIR}/fish_audio_key"
if [ -s "$FISH_KEY_FILE" ]; then
  ok "Fish Audio キーは既に保存済み ($FISH_KEY_FILE)"
  add_report "✅ Fish Audio"
else
  echo
  info "Fish Audio のキー発行ページを開きます（読者が『登録・貼り付け』を担当）。"
  info "   1) 表示されたページで API Key を作成"
  info "   2) 発行されたキーをコピー（Cmd+C）"
  info "   3) このウィンドウに戻ってペースト → Enter"
  info "   ※ 空Enter でスキップ（Macの声=Kyokoで代替。あとで再実行で追加可）"
  echo
  open "https://fish.audio/app/api-keys" 2>/dev/null || true
  # キーをターミナルに表示しない
  stty -echo 2>/dev/null || true
  printf "   キー貼り付け（Enterでスキップ）: "
  read -r FISH_KEY || FISH_KEY=""
  stty echo 2>/dev/null || true
  echo
  if [ -n "${FISH_KEY:-}" ]; then
    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR" 2>/dev/null || true
    printf '%s' "$FISH_KEY" > "$FISH_KEY_FILE"
    chmod 600 "$FISH_KEY_FILE" 2>/dev/null || true
    ok "Fish Audio キーを保存しました（$FISH_KEY_FILE / 権限600）"
    add_report "✅ Fish Audio"
  else
    skip "Fish Audio はスキップ（Macの声で代替されます）"
    add_report "⏭  Fish Audio（Macの声で代替）"
  fi
fi

# ============================================================
# 8) Higgsfield CLI（任意）
# ============================================================
header "8/9 Higgsfield CLI（任意・実映像を使う時のみ）"
NODE_OK=0
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  NODE_OK=1
  ok "Node.js は導入済み ($(node --version))"
fi

if command -v higgsfield >/dev/null 2>&1; then
  ok "higgsfield CLI は導入済み"
  if higgsfield workspace list --json >/dev/null 2>&1; then
    ok "higgsfield 認証済み"
    add_report "✅ Higgsfield（実映像モード可）"
  else
    warn "higgsfield 未認証。実映像を使う時に手動で:  higgsfield auth login"
    add_report "⚠  Higgsfield（要ログイン）"
  fi
else
  if [ "$NODE_OK" -eq 1 ]; then
    echo
    info "Higgsfield を今インストールしますか? （練習モードだけでよいなら不要）"
    printf "   Enter=あとで / y=今すぐ  : "
    read -r ans2 || ans2=""
    if [ "${ans2}" = "y" ] || [ "${ans2}" = "Y" ]; then
      run "npm i -g @higgsfield/cli ..."
      if npm i -g @higgsfield/cli >>"$LOG_FILE" 2>&1; then
        ok "higgsfield CLI 導入完了。後で  higgsfield auth login  で認証してください。"
        add_report "⚠  Higgsfield（要ログイン）"
      else
        warn "npm install 失敗。あとで手動で入れてください。"
        add_report "⏭  Higgsfield（未導入）"
      fi
    else
      skip "Higgsfield は後で導入可（今日は練習モードで動画が作れます）"
      add_report "⏭  Higgsfield（未導入）"
    fi
  else
    skip "Node.js 未導入のためスキップ（実映像を使う時に nodejs.org から）"
    add_report "⏭  Higgsfield（未導入・任意）"
  fi
fi

# ============================================================
# 9) 総仕上げ: 練習動画1本 + Studio 起動
# ============================================================
header "9/9 総仕上げ - 練習動画を1本作って、アプリを開きます"
cd "$REPO_ROOT" || exit 1

run "python run.py --theme \"はじめての練習動画\" --duration 15 --backend mock --no-llm ..."
if "${VENV}/bin/python3" run.py --theme "はじめての練習動画" --duration 15 --backend mock --no-llm >>"$LOG_FILE" 2>&1; then
  LAST_MP4=$(ls -t output/*.mp4 2>/dev/null | head -1 || true)
  if [ -n "${LAST_MP4:-}" ] && [ -s "$LAST_MP4" ]; then
    SIZE=$(du -h "$LAST_MP4" 2>/dev/null | awk '{print $1}')
    ok "練習動画を生成しました: $LAST_MP4 (${SIZE})"
    add_report "✅ 練習動画の生成"
  else
    warn "生成コマンドは終了したが mp4 が見つかりません。ログ: $LOG_FILE"
    add_report "⚠  練習動画（生成物確認できず）"
  fi
else
  err "練習動画の生成に失敗。ログ末尾:"
  tail -5 "$LOG_FILE"
  add_report "⚠  練習動画（失敗）"
fi

# Studio サーバ起動
if lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  ok "Studio サーバは既に 8787 で起動中"
else
  run "Studio サーバをバックグラウンド起動..."
  nohup bash "${REPO_ROOT}/studio/start_server.sh" >>"$LOG_FILE" 2>&1 &
  disown 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then break; fi
    sleep 1
  done
  if curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then
    ok "Studio 起動確認 (http://127.0.0.1:8787/)"
  else
    warn "Studio 起動確認できず。手動で起動: ./studio/start_server.sh"
  fi
fi
add_report "✅ Studio サーバ"

open "http://127.0.0.1:8787/" 2>/dev/null || true

# ============================================================
# 追加: デスクトップに「Reel Studio 起動.command」を自動生成
# 何度実行しても同名で上書き（起動先パスは REPO_ROOT を埋め込み）。
# ============================================================
header "追加 - デスクトップに起動ボタンを設置"
DESKTOP_DIR="$HOME/Desktop"
LAUNCHER="${DESKTOP_DIR}/Reel Studio 起動.command"
if [ -d "$DESKTOP_DIR" ]; then
  # ヒアドキュメントは変数展開する。埋め込む REPO_ROOT のパスは
  # 空白含みでもダブルクォートで固定される。
  cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/bin/bash
# ============================================================
#  Reel Studio 起動ボタン (自動生成)
#  かんたんセットアップ.command が作成しました。
#  ダブルクリックで Studio が起動し、ブラウザが開きます。
# ============================================================
set -uo pipefail
REPO_ROOT="${REPO_ROOT}"
if [ ! -d "\${REPO_ROOT}" ]; then
  echo "アプリのフォルダが見つかりません: \${REPO_ROOT}"
  echo "フォルダを移動した場合は、そのフォルダ内の"
  echo "「かんたんセットアップ.command」をもう一度ダブルクリックしてください。"
  echo "（このボタンが作り直されます）"
  read -r -n 1 _ 2>/dev/null || true
  exit 1
fi
cd "\${REPO_ROOT}" || exit 1

if lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Studio は既に起動しています。ブラウザを開きます..."
else
  echo "Studio を起動しています..."
  nohup bash "\${REPO_ROOT}/studio/start_server.sh" >/tmp/reel_studio_launcher.log 2>&1 &
  disown 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then break; fi
    sleep 1
  done
  if ! curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then
    echo "起動確認ができませんでした。ログ: /tmp/reel_studio_launcher.log"
    echo "「かんたんセットアップ.command」をもう一度実行してみてください。"
    read -r -n 1 _ 2>/dev/null || true
    exit 1
  fi
fi
open "http://127.0.0.1:8787/" 2>/dev/null || true
echo "Reel Studio: http://127.0.0.1:8787/"
LAUNCHER_EOF
  chmod +x "$LAUNCHER" 2>/dev/null || true
  # 自作スクリプトなので通常は quarantine は付かないが念のため外す
  xattr -d com.apple.quarantine "$LAUNCHER" 2>/dev/null || true
  if [ -x "$LAUNCHER" ]; then
    ok "デスクトップにボタンを作りました: 「Reel Studio 起動.command」"
    info "次回からは、このアイコンをダブルクリックするだけで Studio が開きます。"
    info "※ 初回のみ macOS の「開発元を確認できません」警告が出る場合は、"
    info "  右クリック → 開く → 開く で1回だけ許可してください。"
    add_report "✅ デスクトップに起動ボタンを作成"
  else
    warn "デスクトップへの書き込みに失敗しました($LAUNCHER)。"
    add_report "⚠  起動ボタン(デスクトップ書き込み不可)"
  fi
else
  warn "デスクトップフォルダが見つかりません($DESKTOP_DIR)。スキップします。"
  add_report "⏭  起動ボタン(デスクトップ無し)"
fi

echo
echo "${BOLD}============================================================${RESET}"
echo "${BOLD}  セットアップ完了 🎉${RESET}"
echo "${BOLD}============================================================${RESET}"
for line in "${REPORT[@]}"; do
  echo "   $line"
done
echo
info "ブラウザで http://127.0.0.1:8787/ を開きました。"
info "アプリ内でテーマを入力すれば動画が作れます。"
info "デスクトップに「Reel Studio 起動.command」を作りました。"
info "  → 次回からはそのアイコンをダブルクリックするだけで開きます。"
info "困った時は  ${LOG_FILE}  を送ってください。"
info "もう一度実行しても安全（済んだ段はスキップされます）。"
echo
printf "何かキーを押すと閉じます..."
read -r -n 1 _ 2>/dev/null || true
echo
