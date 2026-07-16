# SFX素材の追加方法

このフォルダに効果音ファイル（wav/mp3等）を置き、`manifest.json` に1件ずつエントリを追加してください。
CC0等ライセンスが明確な音源をご自身でご用意ください（本ツールは効果音ファイルを自動DLしません）。

## 同梱されている8種について（検証用プレースホルダー・要リポジトリ復元手順）

`whoosh_01.wav` / `swoosh_01.wav` / `pop_01.wav` / `ding_01.wav` / `click_01.wav` /
`riser_01.wav` / `impact_01.wav` / `sparkle_01.wav` は、SFXオーバーレイ機能が実際に
動作することを実機検証するために `generate_placeholder_sfx.py`（同フォルダ内）で
**ffmpegの信号合成ジェネレータのみから合成した音源**です。外部音源のダウンロードは
一切行っていないため著作権リスクはゼロです。`manifest.json` にも
`license: "synthetic-placeholder"` と明記しています。

**`assets/bgm/*.mp3` と同じ方針で、`*.wav` 本体は `.gitignore` の対象です
（git管理はmanifest.jsonと生成スクリプトのみ）。** クローン直後や `git clean` 後は
このフォルダに `.wav` が存在しないため、SFX機能を使う/検証する前に一度だけ
以下を実行して復元してください（8ファイルを生成し `manifest.json` も上書きします）。

```
"<repo>/.venv/bin/python3" "assets/sfx/generate_placeholder_sfx.py"
```

**ご自身の動画に使う際は、この8種を削除し、下記の手順でお好きなライセンスの明確な
効果音に差し替えてください。**

## manifest.json の形式

```json
[
  {
    "file": "whoosh_01.wav",
    "label_jp": "ホワッシュ（場面転換）",
    "duration": 0.6,
    "license": "CC0"
  }
]
```

- `file`: このフォルダ内のファイル名（相対パス）
- `label_jp`: Studio UIでの表示名（日本語）
- `duration`: 秒（UIでのタイムライン表示・at_secの目安に使用）
- `license`: ライセンス表記（例: `CC0`, `CC-BY 4.0 by ...`）

## SFXが無い場合

`manifest.json` が空配列（`[]`）のままでも、plan.sfx が空リストのままでも、
SFXオーバーレイ工程は丸ごとスキップされ、SFX無しの動画として正常に完成します。
