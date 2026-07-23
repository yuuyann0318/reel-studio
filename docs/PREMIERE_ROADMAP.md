# Premiere連携ロードマップ（xmeml主・UXP補助へ）

higgsfield-auto-reel の Premiere Pro 連携（`premiere/` 一式）の現状アーキテクチャと、
Adobe が推進する UXP プラットフォームへの移行方針をまとめる。

- 最終更新: 2026-07-23
- 対象読者: 本リポジトリの保守者・拡張担当

## 1. 現状アーキテクチャ（2026-07-23 時点）

### 1.1 データフロー

```
studio plan (v2)
   │
   │  build_package(project_id)                 ← premiere/package.py
   ▼
projects/<id>/premiere/<ts>_<uuid>/
   ├── reel.xml         ← FCP7 xmeml       (premiere/export_xmeml.py)
   ├── captions.srt     ← SRT 字幕         (premiere/srt.py)
   ├── timeline.json    ← 機械可読サイドカー(build_xmeml の同期意図の要約)
   ├── narration.wav    ← TTS 音声
   ├── style/STYLE_SPEC.md
   └── README_import.md
   │
   │  run_import(package_dir, project_id)      ← premiere/driver.py（Phase B）
   ▼
Premiere Pro (pymiere 経由の CEP 拡張 = Pymiere Link 前提)
   └── reel.prproj      ← 新規作成 → reel.xml インポート → captions.srt 添付 → 保存
```

- Phase A（xmeml 生成 + README + timeline.json サイドカー）: **手動取り込み前提の中核経路**。
  Premiere/DaVinci/Final Cut が xmeml を読めるので実質どの NLE でも動く。
- Phase B（pymiere 自動インポート）: **オプション経路**。Pymiere Link CEP 拡張が入っている
  環境で自動化する。失敗時は Phase A(＝ユーザーが README 通りに reel.xml を手動 Import)へ
  静かにフォールバックする（`studio/server/jobs.py` の実装契約）。

### 1.2 静的QA（qa/premiere_qa.py・2026-07-23 追加）

Premiere を起動しなくても、書き出しパッケージが構造的に正しいかを機械検証する。

| 検査 | 内容 |
|---|---|
| well_formed | reel.xml が well-formed（xmllint 優先、無ければ ElementTree で代替） |
| xml_timeline_consistency | V2 generatoritem 数=captions 数 / A3 SE 数=sfx 数 / marker 数下限 / A2 BGM keyframe の有無 |
| sync_within_one_frame | shot cumulative の start_sec 一致 / caption 表示区間が shot 内 / A2 keyframe が bgm_curve 重要時刻(hook/cta/dip)と ±1F 一致 |
| plan_parity | project.json の plan v2 と timeline.json の hook_end/cta_start が ±1F 一致 |

## 2. なぜ xmeml 主・pymiere 補助か

### 2.1 xmeml (FCP7 XML)

- **利点**: NLE 非依存の中間表現。Premiere/Final Cut/DaVinci/Resolve が長年サポート。
  テキスト（XML）なので機械検証・差分比較・バージョン管理と相性が良い。
- **弱点**: レガシー扱い。Adobe は「Import 経路」としてはまだ生かしているが、拡張機能で
  読ませる場合の互換性が保証されない領域が増えている（Text ジェネレータの見た目パラメータ、
  高度なエフェクトなど）。

### 2.2 pymiere（+ Pymiere Link）

- **利点**: 「Premiere を Python から叩ける」唯一の実用パス（2024〜25 年時点）。
  reel.xml のインポート、シーケンス走査、SRT 字幕トラック化などが自動化できる。
- **弱点**: **CEP 拡張前提**。CEP は Adobe が UXP へ移行しつつあり、Premiere Pro 2026 では
  デフォルトで CEP 拡張が読み込まれず、既存の Extension Manager インストールも
  `status=-175` で失敗する（2026-07-23 に本機で実測）。
  上流 pymiere リポは 2026-09 以降のメンテを予告しておらず、事実上 EOL。

### 2.3 現状の妥協点

**手動 Import 経路（Phase A）が本線・pymiere 経路（Phase B）はベストエフォート**。
これにより、Premiere Pro 2026 以降で Pymiere Link が使えなくなっても、パッケージ+README で
必ずユーザーが手動 Import できる。

## 3. Premiere Pro 2026 上での実測（2026-07-23）

- pymiere（Python パッケージ）: **導入済み**（`pip show pymiere` 通過）。
- Pymiere Link CEP 拡張: **導入不能**。上流 `extension_installer_mac.sh` は
  ExManCmd 経由で `.zxp` を流し込むが、macOS の Adobe Extension Manager が
  `Extension Manager init failed, status = -175!` で初期化できない。
  これは Adobe 側の UXP 移行に伴う CEP サポート縮小によるもの。
- Adobe Premiere Pro 2026 プロセス: **起動確認済み**（pgrep 実測）。
- `setup_check.check_setup()` の実測結果: `{"ready": False, "missing": ["pymiere_link_unreachable"]}`。
- `driver.run_import()` を無い package_dir で呼ぶと、`{"ok": False, ...reel.xmlが見つからない...}` を
  例外なく返す。**自動フォールバック経路（Phase A）は健全**。

### 3.1 E2E 検証の縮退

Premiere 2026 では Pymiere Link を通した「シーケンス読み戻し」検証は物理的に不可能。
代替として:

- **静的QA（qa/premiere_qa.py）を CI/CD で常時実行**。build_xmeml が生成する
  reel.xml/timeline.json の内部整合を機械検証する。
- **ユーザー実機での手動 Import** を README で案内する。「Premiere でシーケンスが 1080x1920 で
  読める / 字幕がタイムラインへドラッグできる」ことを目視で確認する運用。

## 4. UXP 移行ロードマップ

### 4.1 現状評価

- pymiere: **2026-09 以降 EOL 想定**（メンテナが後継を明言していない）。
- Premiere Pro 2026: **CEP は "deprecated"**。UXP プラグインが公式推奨。
- UXP 側の Python SDK は現時点で存在しない。代わりに `@adobe/premierepro`（TypeScript/Node）が
  提供されており、UXP プラグインとしてバンドルする形。

### 4.2 移行タイミング

| 時期 | 判断基準 | アクション |
|---|---|---|
| 〜2026-09 | pymiere が動く環境（〜Premiere Pro 2025）が過半 | 現状維持（xmeml 主・pymiere 補助）。 |
| 2026-09〜 | pymiere EOL、Premiere Pro 2026 が主流 | Phase B（pymiere）を deprecated 扱い。setup_check は依然として点検を返すが、公式サポートは Phase A のみ。 |
| 2026 Q4〜 | UXP 主流 & Node が使える環境が広まる | UXP プラグイン（`@adobe/premierepro`）版 driver を追加。CEP 版とは並立させ、UXP 版が動く環境では優先。 |
| 2027〜 | 需要が明確化 | MCP（Model Context Protocol）ブリッジ経由の統合（例: [antipaster/Adobe-Premiere-Pro-MCP](https://github.com/antipaster/Adobe-Premiere-Pro-MCP)）を検討。LLM 経由での編集指示自動化と噛み合う。 |

### 4.3 移行工数見積り（大まかな目安）

| 項目 | 見積 | 内訳 |
|---|---|---|
| UXP プラグイン雛形作成 | 2-3 日 | プラグイン manifest / TypeScript ビルド設定 / Premiere が読める形での配布パス確認 |
| xmeml Import 相当 API 実装 | 3-5 日 | `@adobe/premierepro` の Project.importFiles / Sequence 走査に相当する API 呼び分け |
| SRT 字幕トラック化 | 2-3 日 | UXP の Caption API が pymiere.createCaptionTrack と等価か検証、非等価なら手作業手順に一部退化 |
| Python 側の driver 相当（Node 呼出しラッパ） | 2 日 | subprocess で node ブリッジを叩き、成功/失敗を Phase B と同じ dict 形式で返す |
| setup_check の UXP 対応 | 1 日 | UXP プラグインの load 状態を検知する新しい健全性チェック |
| 回帰テスト・BUG_INVENTORY 追記 | 2 日 | pymiere 版と UXP 版の同期一致テストを parity として並列で走らせる |
| **合計** | **12-16 日** | 1 人月弱の見積 |

### 4.4 移行判断のトリガ

以下のいずれかが発生したら UXP 版の着手を開始する:

1. Premiere Pro 2025 以前を使うユーザーが 20% を切ったことをアンケート等で確認
2. `qa/premiere_qa.py` の指摘とは別に、Pymiere Link での不具合 issue が2件以上発生
3. Adobe 公式が CEP を「読み込まない」旨を Premiere Pro のリリースノートで明記

## 5. 参考リンク

- pymiere: https://github.com/qmasingarbe/pymiere
- Adobe UXP for Premiere Pro: https://developer.adobe.com/premiere-pro/uxp/
- @adobe/premierepro (UXP JS API): https://developer.adobe.com/premiere-pro/uxp/reference/premierepro/
- Adobe-Premiere-Pro-MCP: https://github.com/antipaster/Adobe-Premiere-Pro-MCP
- CEP status/deprecation について:
  https://github.com/tmoroney/auto-subs/issues/571 （UXP 移行の第三者観測）

## 6. 開発時のチェックリスト

コードを触るときの最小の心得。

- [ ] `premiere/` 配下を変更したら、`tests/test_premiere_*.py` + `tests/test_premiere_qa.py` を必ず走らせる（pytest 全緑必須）。
- [ ] xmeml のトラック規約（V1本編/V2テロップ/V3演出/A1ナレ/A2 BGM/A3 SE）を守る。増減する場合は `qa/premiere_qa.py` の期待値と `premiere/package.py` の README 文言を同時に更新。
- [ ] `bgm_curve` の重要時刻（hook_end/cta_start/dip_events）を変えたら、`test_premiere_sync_parity.py` の parity 期待値を更新。
- [ ] `driver.run_import` の I/O 依存を増やしたら、`setup_check` に新チェック（`missing` 種別を1つ）を追加して安全側にフォールバック可能に。
- [ ] UXP 版を追加するときは、既存 Phase B と並立させる（削除しない・優先度で切り替え）。
