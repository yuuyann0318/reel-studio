# テロップ スタイル仕様（STYLE_SPEC）

Premiereの「トラックスタイル」ファイル（`.prtextstyle`）はAdobeの内部バイナリ形式のため、
このツールから自動生成できません。代わりに、この仕様書のとおりにPremiere上でテキスト
スタイルを作成し、「トラックスタイルとして保存」してください。一度保存すれば、次回以降は
ワンクリックで使い回せます（README_import.md の手順4を参照）。

## 文字

- フォント: Noto Sans JP Black（無ければ最も太いゴシック体で代替）
- 文字色: 白 (#FFFFFF)
- 縁取り（ストローク）: 黒 (#000000)、細め（目安: フォントサイズの約5%の太さ）
- 配置: 画面中央よりやや上（画面中心から上に約15%）
- 揃え: 中央揃え
- 最大行数: 2行

## サイズの目安（1080x1920 / 9:16 縦動画基準）

- フォントサイズ: 画面の縦幅の約7%（1080x1920なら目安 130px前後。読みにくければ調整可）
- 行間: フォントサイズの1.2倍程度
- セーフエリア: 画面左右10%・下20%には文字がかからないようにする

## テロップごとの見た目（style_detail: 参考動画からの高解像度TTP）

参考動画の各テロップは vision 解析で `style_detail` として1枚ずつ精密抽出され、xmeml の
`<generatoritem>` に**テロップ単位で**焼き込まれます（`premiere.export_xmeml._resolve_hint_for_xmeml`）。
Premiere の汎用 Text ジェネレータは全パラメータを厳密には反映しないため、下表を「トラック
スタイル作成時の狙い」として使ってください。ASS 焼き込み版（自動生成MP4）はこの style_detail を
libass で忠実に再現しています（フォント/色/縁/位置/サイズ/背景帯）。

| style_detail | 意味 | xmeml への反映 | 実フォント/値の対応 |
|---|---|---|---|
| font_class | 書体系統 | `<parameterid>font` | gothic→Noto Sans JP Black / mincho→Noto Serif JP Black / rounded→Zen Maru Gothic Black / pop→Mochiy Pop One / handwriting→Klee One SemiBold |
| weight | 太さ | （ASS は \b / Premiere はフォント選択） | normal/bold/heavy（同梱フォントは主に Black 単一ウェイトのため weight 忠実度は粗い） |
| fill_color_hex | 文字色 | `<parameterid>fontcolor`(#RRGGBB) | そのまま |
| stroke_color_hex / stroke_width | 縁の色・太さ | （ASS は \3c+\bord。Premiere は .prtextstyle 側で設定） | none/thin(≈3px)/thick(≈9px) |
| bg / bg_color_hex | 背景（箱/帯） | （ASS は BorderStyle=3 相当の矩形レイヤ。Premiere は背景シェイプで代替） | none/box(文字幅)/band(全幅帯) |
| pos_x / pos_y_pct | 水平・垂直位置 | `<parameterid>position`（top_safe/center/bottom_safe） | pos_y_pct<34→上, <67→中央, それ以上→下 |
| size_pct | 文字高（画面高%） | `<parameterid>size`（px = size_pct/100×1920×0.7） | 例: 7%→約94px |
| line_count | 行数 | 折り返しの目安 | — |

font_class→実フォントの対応は `pipeline/font_map.py`（`assets/fonts` 同梱・libass 実測で
`\fn` マッチ確認済みのファミリのみ採用）。旧 telop（style_detail 無し）は従来どおり
position/color/size_class の3属性から解決します（後方互換）。

## 参考

本仕様は `assets/profiles/ttp_reference.json` の `telop` セクション（参考動画のTTP解析値）
に基づきます。動画ごとに個別調整したい場合は、`assets/profiles/manual_drive.json` を
作成して上書きしてください（`premiere.profile.load_profile` が自動でマージします）。
