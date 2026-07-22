# BGM inbox（自動取り込みフォルダ）

このフォルダに **mp3 / wav / m4a** を置いて `pipeline.bgm_library.import_inbox()` を実行すると、
`assets/bgm/library/<slug>.m4a` に loudnorm(-18 LUFS) 正規化して取り込み、`manifest.json` に自動追記されます。
原本ファイルは `imported/` へ移動されます。

## ファイル名でムード指定（先頭トークン）

区切りは `_` / `-` / 半角スペース。先頭トークンが以下の既知ムードなら自動判定、未知なら `any`（どの mood 要求でも fallback 候補）。

- `upbeat` / `calm` / `emotional` / `dramatic` / `lofi`

例:
- `upbeat_夏っぽい.mp3` → mood="upbeat"
- `calm-piano-night.wav` → mood="calm"
- `random_song.m4a` → mood="any"

## 手動取り込みコマンド

プロジェクトルート (`reel-studio/`) で以下を実行してください。

```bash
./.venv/bin/python3 -c "from pipeline import bgm_library; print(bgm_library.import_inbox())"
```

## 重複スキップ

sha1 で内容一致を判定するため、同じファイルを2度置いてもスキップされます。
