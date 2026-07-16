# BGM素材の追加方法

このフォルダにBGM音源ファイル（mp3/wav等）を置き、`manifest.json` に1曲ずつエントリを追加してください。
CC0等ライセンスが明確な曲をご自身でご用意ください（本ツールはBGMファイルを自動DLしません）。

## 現在同梱されている3曲について（検証用プレースホルダー）

`upbeat_01.mp3` / `calm_01.mp3` / `emotional_01.mp3` は、BGM＋ダッキング機能が実際に
動作することを実機検証するために `generate_placeholder_bgm.py`（同フォルダ内）で
**ffmpegの正弦波ジェネレータのみから合成した音源**です。外部楽曲のダウンロードは
一切行っていないため著作権リスクはゼロですが、実際の楽曲ではない簡易な和音の
繰り返しです。`manifest.json` にも `license: "synthetic-placeholder"` と明記しています。

**ご自身の動画に使う際は、この3曲を削除し、下記の手順でお好きなライセンスの明確な
楽曲に差し替えてください。** 差し替え手順（ファイルを置く→`manifest.json`を書き換える）
は既存の3曲でも新規追加でも同じです。再生成が必要な場合は
`".venv/bin/python3" "assets/bgm/generate_placeholder_bgm.py"` を実行してください
（3曲を上書き再生成します）。

## manifest.json の形式

```json
[
  {
    "file": "upbeat_01.mp3",
    "mood": "upbeat",
    "license": "CC0",
    "loop_ok": true
  }
]
```

- `file`: このフォルダ内のファイル名（相対パス）
- `mood`: `upbeat` / `calm` / `emotional` のいずれか（`edit_plan.json` の `bgm_mood` とマッチングに使われます）
- `license`: ライセンス表記（例: `CC0`, `CC-BY 4.0 by ...`）
- `loop_ok`: ループ再生前提で作られた曲かどうか（`true`/`false`）

## BGMが無い場合

`manifest.json` が空配列（`[]`）のままでも、あるいは `bgm_mood` に一致する曲が無い場合でも、
BGM工程は丸ごとスキップされ、BGM無しの動画として正常に完成します（DESIGN.md §4.5参照）。
