# higgsfield-auto-reel

テーマ文字列1つから、9:16縦型SNSショート動画(mp4)を全自動で1本生成するパイプライン。

`video-auto-editor`（既存動画のAI編集）の姉妹プロジェクトで、こちらは「素材動画が
無い状態からゼロで作る」パターン（企画→ビジュアル生成→ナレーション→字幕→BGM→
最終レンダリング→QA→コンプライアンス検査）を担当する。

## セットアップ

```bash
cd "/Users/yuuya/claude code/higgsfield-auto-reel"
/usr/bin/python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

同梱の `bin/ffmpeg` / `bin/ffprobe` を使うため、システムにffmpegが無くても動く
（`config.json` の `ffmpeg_bin`/`ffprobe_bin` がこのパスを指す）。

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

### higgsfield（未検証・要CLI仕様確認）

`pipeline/visual/higgsfield_backend.py` は Higgsfield CLI（想定: `npm i -g @higgsfield/cli`）
をsubprocessで呼び出す実装。**本実装時点(2026-07-17)ではCLIの実サブコマンド名・
引数・ジョブID/ステータスJSONの形式を未確認**。`_build_generate_cmd()` /
`_build_status_cmd()` / `_build_download_cmd()` / `_parse_job_id()` / `_parse_status()`
の5関数にコマンド構築・レスポンス解釈を隔離してあるので、実際に使う前に

```bash
npm i -g @higgsfield/cli
higgsfield auth login   # ユーザー本人が実行（なりすまし不可）
higgsfield --help
higgsfield generate --help
```

を実行して仕様を確認し、上記5関数だけを実際の仕様に合わせて修正すること。
CLI未インストール時は「`npm i -g @higgsfield/cli && higgsfield auth login`を実行してください」
という明確なエラーメッセージを返す（黙って失敗しない）。

### cloudapi（未実装スタブ）

`pipeline/visual/cloudapi_backend.py` はBearer認証REST API直叩きの雛形。
エンドポイント仕様が確定していないため、呼び出すと`NotImplementedError`を送出する。

## config.json

```json
{
  "backend": "mock",
  "claude_bin": "/Users/yuuya/.local/bin/claude",
  "claude_model": "claude-fable-5",
  "resolution": [1080, 1920],
  "target_duration_sec": 30,
  "voice": "Kyoko",
  "brand_rules": {"ng_words": ["絶対稼げる", "100%成功", "みお", "@mio_ai_insta_", ...]}
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

- **higgsfield_backend.py は未検証。** Higgsfield CLIの実サブコマンド名・JSON形式を
  確認していないため、実際に`--backend higgsfield`で動かす前に上記の仕様確認が必須。
- **cloudapi_backend.py は未実装スタブ。** エンドポイント仕様確定後に実装する。
- ナレーション音声とショットの表示区間の同期は、TTSの単語単位タイムスタンプではなく
  「ショットごとの想定尺(duration_sec)」に基づく近似（`say`コマンドは単語タイムスタンプを
  返さないため、精密な音声認識ベースの同期は未実装）。
- BGMは`video-auto-editor/assets/bgm/`のプレースホルダ音源（sine波合成、著作権フリー）を
  そのまま流用している。本番配布前にライセンスの明確な楽曲へ差し替えること。
