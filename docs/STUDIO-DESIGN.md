# Reel Studio 設計書（窓口確定・v1）

## 目的
higgsfield-auto-reel を「テーマ→自動生成」から「**生成 + 完全編集**（カット/テロップ/BGM/効果音）」へ拡張する。ローカルWebアプリ（モダンUI/UX・日本語）。

## 構成
```
higgsfield-auto-reel/
  studio/
    server/app.py        # FastAPI（.venv に fastapi/uvicorn 追加）
    server/projects.py   # プロジェクト永続化（projects/<id>/project.json）
    server/jobs.py       # 非同期ジョブ+SSE進捗（video-auto-editor/server/ を踏襲）
    web/                 # 静的SPA（ビルド不要・ES modules + modern CSS）
      index.html app.js api.js components/ styles/
  assets/sfx/            # 効果音 8種 + manifest.json（ffmpeg合成プレースホルダ、license明記）
  projects/              # プロジェクトデータ（gitignore）
```

## データモデル: project.json
```json
{
  "id": "p_xxx", "theme": "…", "created_at": "…", "status": "draft|generating|ready|rendering|failed",
  "plan": {
    "shots": [{"id":"s1","order":0,"enabled":true,"prompt":"…","caption":"…",
               "clip_path":"…","source_duration":5.0,
               "trim":{"start":0.0,"end":5.0}}],
    "narration_text": "…",
    "bgm": {"file":"upbeat_01.mp3","gain_db":-14,"ducking":true},
    "sfx": [{"file":"whoosh_01.wav","at_sec":2.5,"gain_db":-8}],
    "subtitle_style": {"font_size":72,"accent_color":"#FFD84D","position":"lower"}
  },
  "renders": [{"path":"…","ts":"…","ok":true}]
}
```

## API 契約（フロント/バック共通・厳守）
- `POST /api/projects` `{theme,duration,backend}` → `{id}`（202、生成ジョブ開始）
- `POST /api/projects/import`（multipart mp4）→ 1クリップのプロジェクト作成（ingestで正規化）
- `GET /api/projects` → 一覧（id,theme,status,thumb）
- `GET /api/projects/{id}` → project.json 全体
- `PUT /api/projects/{id}/plan` → plan 差し替え（検証: trim範囲/参照ファイル存在/コンプラNGワード）
- `POST /api/projects/{id}/render` → 再レンダリングジョブ開始 → `{job_id}`
- `GET /api/jobs/{job_id}/events` → SSE（`{stage,progress,message}`、終了は `{done:true,ok,path}`）
- `GET /api/assets/bgm` / `GET /api/assets/sfx` → manifest 配列
- `GET /media/...` → クリップ/レンダー/アセットの静的配信（Range対応）
- エラーは `{error:{code,message}}` + 適切な4xx/5xx

## レンダラ拡張（pipeline/render.py）
- ショットごとの trim（-ss/-to 正確カット）→ 正規化(1080x1920/30fps) → enabled のみ order 順に連結
- テロップ: plan.shots[].caption + subtitle_style から ASS 再生成（既存 subtitles.py 流用・文節改行維持）
- BGM: gain_db 反映 + 既存ダッキング
- SFX: 各 sfx を at_sec に amix でオーバーレイ（gain_db 反映、出力尺は変えない）
- ラウドネス正規化は最終段（既存 loudnorm）

## UI/UX（プロ水準・ダーク基調）
- 3ペイン+タイムライン: 左=プロジェクト/アセット、中央=9:16プレビュー(video)、右=インスペクタ（選択ショットの caption/trim/enabled、BGM/SFX/スタイル）、下=タイムライン（ショットブロック横並び・トリムハンドル・SFXマーカー・BGMトラック帯）
- デザイントークン: `--bg:#0F1115 --panel:#181B22 --border:#2A2F3A --text:#E8EAF0 --accent:#6C8CFF --accent2:#FFD84D`、Inter + Noto Sans JP、radius 10px、フォーカスリング必須
- 操作: ショット選択→インスペクタ編集→「保存」(PUT plan)→「書き出し」(render)→SSEで進捗バー→完成プレビュー切替
- 空状態/ローディング/エラーの3状態を全ビューで設計。日本語UI。トースト通知。
- 禁止: 「みお」「@mio_ai_insta_」言及、景表法NG表現のサンプル

## 検証
- pytest（API/レンダラ拡張/SFXミックスの単体）
- 実E2E: mock生成→plan編集（カット1・テロップ変更1・SFX2発・BGM差替）→render→ffprobe＋フレーム目視
- UI: Chrome実機でスクリーンショット検収（窓口）
- codex-review → BUG_INVENTORY 追記 → 修正 → 再検証（再帰）
