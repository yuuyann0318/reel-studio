# higgsfield-auto-reel (Reel Studio)

参考動画URL1つから、その構成（カット割り・テロップ・効果音の置き方）を解析して
9:16縦型SNSショート動画(mp4)を全自動で1本生成するパイプライン。

## はじめての人へ（かんたんコース）

1. このページ上の緑の「Code」→「Download ZIP」でダウンロードして解凍
2. フォルダ内の **「かんたんセットアップ.command」をダブルクリック**（macOS専用）
3. あとは画面の指示に従うだけ。詳しくは下のガイドへ:

- セットアップガイド: https://yuuyann0318.github.io/reel-studio-manual/setup.html
- 使い方マニュアル: https://yuuyann0318.github.io/reel-studio-manual/

## セットアップ（手動でやりたい人向け）

```bash
# <設置先> は自分でこのリポジトリを展開したパスに置き換える(例: ~/reel-studio)。
# 空白を含まないパス推奨(スペース入りだと bash 経由で常時ダブルクォート必須になり事故の元)。
cd <設置先>
/usr/bin/python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

同梱の `bin/ffmpeg` / `bin/ffprobe` を使うため、システムにffmpegが無くても動く
（`config.json` の `ffmpeg_bin`/`ffprobe_bin` がこのパスを指す）。

### 初回セットアップ（SFX / BGM ライブラリの生成）

SFX / BGM の生成物（`assets/sfx/*_gen_*.wav`, `assets/bgm/library/*.m4a`）は
`.gitignore` によりリポジトリにコミットされない。clone / pull 直後は空なので、
以下を1回実行してローカルに生成する（合計 3〜5 分・ffmpegのみ・外部通信なし）。

```bash
.venv/bin/python assets/sfx/generate_sfx_library.py \
  && .venv/bin/python assets/bgm/generate_starter_pack.py
```

生成物は非コミット前提のため、SFX/BGM関連のテストは生成物が無ければ `pytest.skip`
される。テストが skip されたら上記スクリプトを流してから `pytest -q` を再実行する。

## 使い方

```bash
source .venv/bin/activate

# Mockバックエンド（無課金・鍵不要・常にE2Eが通る）＋ LLM企画生成あり
python run.py --theme "AIで副業を始める最初の一歩" --duration 30 --backend mock

# LLMを使わず決定論的テンプレートで即座に生成（claude CLI不通時の保険）
python run.py --theme "AIで副業を始める最初の一歩" --duration 20 --backend mock --no-llm
```

- 完成mp4: `output/<run_id>.mp4`
- 実行レポート（各段の所要時間・採用判断・QA結果）: `output/report.json`
- 中間ファイル（企画JSON・クリップ・字幕ASS等）: `work/<run_id>/`

### オプション

| オプション | 説明 |
|---|---|
| `--theme` (必須) | 動画のテーマ文字列 |
| `--duration` | 目標尺(秒)。未指定は`config.json`の`target_duration_sec` |
| `--backend` | `mock` / `higgsfield` / `cloudapi`（未指定は`config.json`の`backend`） |
| `--aspect` | 現状 `9:16` のみ対応 |
| `--no-llm` | claude CLIを使わず決定論的テンプレートで企画生成する |
| `--quality` | `supreme`(既定・write+polish) / `single`(一発) / `supreme_plus`(write+polish+rewrite・品質最優先) |
| `--match-reference-duration` | 目標尺を参考動画の実尺に強制一致させる(参考のリズムをそのまま保つ) |
| `--reference-url` | 参考動画URL(TikTok等)。LLM経路では必須 |

### 品質最優先モード（時間かけていい派）

`config.local/quality_max.json` を置くと自動的に `--quality supreme_plus` プリセットが
有効化される（`config.local/` は `.gitignore` 済み・コミット対象外）。テンプレは
`config.local.sample/quality_max.json` に同梱：

```bash
cp config.local.sample/quality_max.json config.local/quality_max.json
python run.py --theme "..." --reference-url "https://..." --quality supreme_plus --match-reference-duration
```

生成後は `output/<run_id>/fidelity.json` に参考動画TTP再現度の5指標
(cut/telop_iou/telop_style/sfx/camera_move) が保存される。

## パイプライン構成（8段）

1. **director** (`pipeline/director.py`): テーマ→企画(コンセプト/フック/ナレーション台本/
   ショットリスト)をFable5(`claude` CLI)で生成。`pipeline/plan_schema.py`で検証し、
   不合格ならエラーを付けた矯正プロンプトで再帰リトライ(最大2回)。claude CLI不通時 /
   `--no-llm`指定時は決定論的テンプレート(`plan_schema.build_rule_based_plan`)にフォールバック。
2. **compliance** (`pipeline/compliance.py`): 台本・キャプションのNGワード検査
   （景品表示法NG表現・競合名義）。違反があればここでrunを停止し、レンダリングへ進まない。
3. **visual** (`pipeline/visual/`): ショットごとに`VisualBackend.generate()`でクリップ生成し、
   `pipeline/render.build_normalize_clip_cmd`で1080x1920/30fps/音声なしへ正規化。
4. **concat**: 正規化済みクリップをconcat demuxerで単純連結。
5. **tts** (`pipeline/tts.py`): ナレーション台本を音声化。既定は macOS `say -v Kyoko`。
   `say`/voiceが無い環境では無音トラック(文字数から尺概算)にフォールバックし、
   字幕主体で完成させる。
6. **subtitles** (`pipeline/subtitles.py`): 各ショットの表示区間に同期したASS字幕
   （1080x1920、13字禁則改行、hookショットは強調スタイル）。
7. **render** (`pipeline/render.py`): 字幕焼込＋ナレーション＋BGMダッキング＋
   loudnorm 2パス（I=-14 LUFS, TP<=-1.0）＋最終H.264/AACエンコード。
8. **qa** (`qa/qa_check.py`): `bin/ffprobe`/`bin/ffmpeg`の実測値で
   解像度==1080x1920 / 尺が目標±15% / 音声ストリーム有 / 黒フレームゼロ / ファイルサイズ>0 を検査。

各段は例外を握って`output/report.json`に記録し、途中失敗でも`work/<run_id>/`に
部分成果物が残る。

## ビジュアルバックエンド

`config.json`の`"backend"`で切替える。

### mock（既定・必須・完全動作）

`bin/ffmpeg`の`gradients`ソースフィルタ＋`zoompan`（パン/ズーム/Ken Burns風）＋
`drawtext`でキャプションを焼いた擬似クリップを生成する。外部API・課金・鍵が一切不要で、
常にE2Eが通ることを保証する。本プロジェクトの自己検証はこのバックエンドで実施済み。

### higgsfield（実CLI仕様確認済み・2026-07-17）

`pipeline/visual/higgsfield_backend.py` は Higgsfield CLI（`higgsfield` コマンド、
確認時バージョン `1.1.17`）をsubprocessで呼び出す実装。実機で確認済みのフローは
以下の3段階（コマンド構築・レスポンス解釈は `_build_*_cmd()` / `_parse_*()` に隔離）。

1. コスト見積: `higgsfield generate cost <model> --prompt "..." --aspect-ratio 9:16
   --resolution 480p --duration <int> --json` → `{"credits": N}`。
   `config.json` の `higgsfield.max_credits_per_shot`（既定10）を超える見積の場合は
   **ジョブを投入せず中断**する（課金安全弁。`HiggsfieldCostLimitError`）。
2. ジョブ投入: `higgsfield generate create <model> --prompt "..." --aspect-ratio 9:16
   --resolution 480p|720p --duration <int秒> --json` → job_idのJSON配列
   （例 `["2296cac2-..."]`）。
3. 完了待ち: `higgsfield generate wait <job_id> --timeout <N>s --interval <N>s --json`
   → `status=="completed"` で `result_url` にmp4のURL。`curl` でダウンロードして
   out_pathへ保存する。

**セットアップ手順**:

```bash
# Node.js が未導入なら先に入れる: https://nodejs.org/ から LTS(推奨)、または brew install node
# その後、Higgsfield CLI を公式手順で導入
npm install -g @higgsfield/cli
which higgsfield                   # インストール先パスを確認
higgsfield auth login              # ユーザー本人が実行(なりすまし不可・初回のみ)
higgsfield workspace list --json   # ワークスペース(プラン/クレジット残高)を確認
higgsfield workspace set <workspace_id>   # 未選択なら選択
```

コード側は `shutil.which("higgsfield")` で PATH から解決する。
制作環境固有の追加パス（開発機のNode配布パス等）に依存させないため、
一般的な導入(nodejs.org / brew)で PATH に載っていれば追加設定不要。

**エラー分類**: `HiggsfieldAuthError`（stderrに"Not authenticated"）/
`HiggsfieldTimeoutError`（CLI自身のタイムアウト報告 or Python側subprocessタイムアウト）/
`HiggsfieldJobFailedError`（status=="failed"）/ `HiggsfieldCostLimitError`（課金安全弁）を
それぞれ明確なメッセージで区別する（すべて`VisualBackendError`のサブクラス）。

**実測**: `resolution: "480p"`, `aspect_ratio: "9:16"` を指定すると、実際の出力は
**496x864**（h264, 24fps, aac音声付き）で返る（1080x1920ではない）。そのため
`pipeline/render.build_normalize_clip_cmd`（scale+crop+fps+format）による
1080x1920への正規化を、mock/higgsfieldどちらの出力に対しても必ず経由させている
（`run.py` Stage 3で全バックエンド共通）。

**既定モデル**: `seedance_2_0_mini`（video, aspect_ratio="9:16"対応,
resolution=480p/720p, durationは整数秒, generate_audio=true既定）。
`config.json` の `higgsfield.model` で変更可能。

### cloudapi（未実装スタブ）

`pipeline/visual/cloudapi_backend.py` はBearer認証REST API直叩きの雛形。
エンドポイント仕様が確定していないため、呼び出すと`NotImplementedError`を送出する。

## config.json

```json
{
  "backend": "mock",
  "claude_bin": "<`which claude` の結果を貼る・例: $HOME/.local/bin/claude>",
  "claude_model": "claude-fable-5",
  "resolution": [1080, 1920],
  "target_duration_sec": 30,
  "voice": "Kyoko",
  "brand_rules": {"ng_words": ["絶対稼げる", "100%成功", ...]},
  "higgsfield": {
    "cli_bin": "higgsfield",
    "model": "seedance_2_0_mini",
    "resolution": "480p",
    "max_credits_per_shot": 10,
    "poll_interval_sec": 5,
    "poll_timeout_sec": 600
  }
}
```

## テスト

```bash
source .venv/bin/activate
python -m pytest -q
```

`@pytest.mark.slow`を付けたテスト（`test_mock_backend_generate_produces_valid_clip`）は
実機`bin/ffmpeg`で1.5秒の小尺クリップを実際に生成して検証する。それ以外は外部バイナリに
依存しない純粋なユニットテスト（`build_*`/`judge_*`関数）。

## 既知の未完・リスク

- **higgsfield_backend.py は実CLI仕様確認済み・1ショットの実機生成で検証済み**
  （2026-07-17。5秒/480p/9:16で実生成→ダウンロード→ffprobe実測まで確認）。
  ただし複数ショットのフルリール生成でのコスト・安定性（レート制限等）は未検証。
- **cloudapi_backend.py は未実装スタブ。** エンドポイント仕様確定後に実装する。
- ナレーション音声とショットの表示区間の同期は、TTSの単語単位タイムスタンプではなく
  「ショットごとの想定尺(duration_sec)」に基づく近似（`say`コマンドは単語タイムスタンプを
  返さないため、精密な音声認識ベースの同期は未実装）。
- BGMは`video-auto-editor/assets/bgm/`のプレースホルダ音源（sine波合成、著作権フリー）を
  そのまま流用している。本番配布前にライセンスの明確な楽曲へ差し替えること。
