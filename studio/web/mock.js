// Reel Studio — フロント内蔵モック（?mock=1 で有効）
// バックエンド無しで全UI状態（一覧/生成中/編集/書き出し中/エラー）を検証するためのダミー実装。
// 実APIと同じ形状のデータ・同じ関数シグネチャを返す。SSEは setInterval による擬似進捗。

import { ApiError } from "./api.js";

const qs = new URLSearchParams(location.search);
let seq = 1;
function genId(prefix) { return `${prefix}_${Date.now().toString(36)}${(seq++).toString(36)}`; }
function nowIso() { return new Date().toISOString(); }

// ---------- アセット manifest（実 assets/bgm/manifest.json と同形状） ----------
const BGM_ASSETS = [
  { file: "upbeat_01.mp3", mood: "upbeat", license: "synthetic-placeholder（本番配布前に要差し替え）", loop_ok: true },
  { file: "calm_01.mp3", mood: "calm", license: "synthetic-placeholder（本番配布前に要差し替え）", loop_ok: true },
  { file: "emotional_01.mp3", mood: "emotional", license: "synthetic-placeholder（本番配布前に要差し替え）", loop_ok: true },
];

const SFX_ASSETS = [
  { file: "whoosh_01.wav", category: "impact", license: "synthetic-placeholder", duration_sec: 0.6 },
  { file: "ding_01.wav", category: "ui", license: "synthetic-placeholder", duration_sec: 0.4 },
  { file: "pop_01.wav", category: "ui", license: "synthetic-placeholder", duration_sec: 0.3 },
  { file: "swipe_01.wav", category: "transition", license: "synthetic-placeholder", duration_sec: 0.5 },
  { file: "click_01.wav", category: "ui", license: "synthetic-placeholder", duration_sec: 0.2 },
  { file: "chime_01.wav", category: "impact", license: "synthetic-placeholder", duration_sec: 0.8 },
  { file: "alert_01.wav", category: "impact", license: "synthetic-placeholder", duration_sec: 0.5 },
  { file: "transition_01.wav", category: "transition", license: "synthetic-placeholder", duration_sec: 0.7 },
];

function defaultSubtitleStyle(preset) {
  return { font_size: 72, accent_color: "#FFD84D", position: "lower", preset: preset || "default" };
}

function buildShots(theme, count) {
  const templates = [
    `${theme}、知っていますか？`,
    "実は、今日からでも始められます",
    "むずかしく考えなくて大丈夫",
    "小さな一歩が、大きな変化につながります",
    "続ける仕組みさえあれば大丈夫",
    "まずは、できることから始めましょう",
    "今日が、あなたの新しいスタートです",
  ];
  const shots = [];
  for (let i = 0; i < count; i++) {
    const dur = 4 + (i % 3) * 0.5;
    shots.push({
      id: `s${i + 1}`,
      order: i,
      enabled: true,
      prompt: `${theme} / シーン${i + 1}`,
      caption: templates[i % templates.length],
      clip_path: `mock/${genId("clip")}.mp4`,
      source_duration: dur,
      trim: { start: 0, end: dur },
    });
  }
  return shots;
}

// ---------- シード用プロジェクト ----------
const DB = { projects: new Map(), jobs: new Map() };

function seed() {
  // 1) 編集済み・レンダー可能なプロジェクト（5ショット・1件disabled・BGM+SFX2発）
  const p1shots = buildShots("AIで副業を始める最初の一歩", 5);
  p1shots[0].caption = "スマホ1台で始められる副業、知っていますか？";
  p1shots[1].caption = "AIを使えば、経験がなくても始められます";
  p1shots[2].caption = "まずは小さな一歩から。今日、調べてみましょう";
  p1shots[2].enabled = false; // 無効ショットの表示デモ
  p1shots[2].trim = { start: 0.5, end: 5.0 };
  p1shots[3].caption = "続ける仕組みさえあれば、誰でも積み上げられます";
  p1shots[3].trim = { start: 0, end: 4.0 };
  p1shots[4].caption = "今日が、あなたの新しいスタートです";

  DB.projects.set("p_ready001", {
    id: "p_ready001",
    theme: "AIで副業を始める最初の一歩",
    created_at: "2026-07-15T09:12:00+09:00",
    status: "ready",
    plan: {
      shots: p1shots,
      narration_text: p1shots.map((s) => s.caption).join("。"),
      bgm: { file: "upbeat_01.mp3", gain_db: -14, ducking: true },
      sfx: [
        { file: "whoosh_01.wav", at_sec: 2.0, gain_db: -8 },
        { file: "ding_01.wav", at_sec: 12.5, gain_db: -6 },
      ],
      subtitle_style: defaultSubtitleStyle(),
    },
    renders: [],
  });

  // 2) 生成中プロジェクト（ジョブが走っている状態を再現）
  const genId1 = "p_gen002";
  DB.projects.set(genId1, {
    id: genId1,
    theme: "毎日5分でできる片付け習慣",
    created_at: nowIso(),
    status: "generating",
    plan: null,
    renders: [],
  });
  startGenerateJob(genId1, "毎日5分でできる片付け習慣", 30);

  // 3) 3件目（一覧のバリエーション用・ready・ショット少なめ）
  const p3shots = buildShots("ヤフオク仕入れ副業のはじめかた", 3);
  DB.projects.set("p_ready003", {
    id: "p_ready003",
    theme: "ヤフオク仕入れ副業のはじめかた",
    created_at: "2026-07-10T21:03:00+09:00",
    status: "ready",
    plan: {
      shots: p3shots,
      narration_text: p3shots.map((s) => s.caption).join("。"),
      bgm: { file: "calm_01.mp3", gain_db: -16, ducking: true },
      sfx: [],
      subtitle_style: defaultSubtitleStyle(),
    },
    renders: [],
  });
}

function startGenerateJob(projectId, theme, durationSec, style) {
  const stages = [
    { stage: "queued", message: "キューに登録しました", pct: 0 },
    { stage: "script", message: "台本を生成しています…", pct: 18 },
    { stage: "narration", message: "ナレーションを生成しています…", pct: 42 },
    { stage: "visual", message: "ショット素材を生成しています…", pct: 78 },
    { stage: "compliance", message: "コンプライアンスチェック中…", pct: 94 },
  ];
  const job = {
    id: projectId, // 契約: 生成ジョブの id はプロジェクト作成レスポンスの {id} を使う
    kind: "generate",
    projectId,
    stepIndex: 0,
    stages,
    startedAt: Date.now(),
    done: false,
  };
  job.onFinish = () => {
    // vertical_hookは高速カット構成（尺÷2秒目安）、それ以外は従来どおり尺÷5秒目安。
    const shotCount = style === "vertical_hook"
      ? Math.max(3, Math.round(durationSec / 2))
      : Math.max(3, Math.round(durationSec / 5));
    const project = DB.projects.get(projectId);
    if (!project) return;
    project.status = "ready";
    project.plan = {
      shots: buildShots(theme, shotCount),
      narration_text: `${theme}についての台本です。`,
      bgm: { file: "calm_01.mp3", gain_db: -14, ducking: true },
      sfx: [{ file: "whoosh_01.wav", at_sec: 2.0, gain_db: -8 }],
      subtitle_style: defaultSubtitleStyle(style),
    };
  };
  DB.jobs.set(job.id, job);
  return job;
}

function startRenderJob(projectId) {
  const stages = [
    { stage: "queued", message: "書き出しキューに登録しました", pct: 0 },
    { stage: "trim", message: "カット・トリムを適用しています…", pct: 22 },
    { stage: "subtitle", message: "テロップを焼き込んでいます…", pct: 48 },
    { stage: "audio", message: "BGM/SFXをミックスしています…", pct: 74 },
    { stage: "loudnorm", message: "音量を正規化しています…", pct: 92 },
  ];
  const jobId = genId("job_render");
  const job = { id: jobId, kind: "render", projectId, stepIndex: 0, stages, startedAt: Date.now(), done: false };
  job.onFinish = () => {
    const project = DB.projects.get(projectId);
    if (!project) return;
    project.status = "ready";
    project.renders = project.renders || [];
    project.renders.push({ path: `mock/render_${Date.now()}.mp4`, ts: nowIso(), ok: true });
  };
  DB.jobs.set(jobId, job);
  return job;
}

seed();

// ---------- 疑似レイテンシ ----------
function delay(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---------- 公開関数（api.js と同じシグネチャ） ----------
export async function listProjects() {
  await delay(180);
  return Array.from(DB.projects.values())
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    .map((p) => ({
      id: p.id,
      theme: p.theme,
      status: p.status,
      thumb: null,
      created_at: p.created_at,
    }));
}

export async function getProject(id) {
  await delay(120);
  const p = DB.projects.get(id);
  if (!p) throw new ApiError("PROJECT_NOT_FOUND", `プロジェクトが見つかりません: ${id}`, 404);
  return JSON.parse(JSON.stringify(p));
}

export async function createProject({ theme, duration, backend, style }) {
  await delay(150);
  if (!theme || !theme.trim()) {
    throw new ApiError("VALIDATION_ERROR", "テーマを入力してください", 422);
  }
  const id = genId("p");
  DB.projects.set(id, {
    id,
    theme: theme.trim(),
    created_at: nowIso(),
    status: "generating",
    plan: null,
    renders: [],
    backend: backend || "mock",
    style: style || "default",
  });
  startGenerateJob(id, theme.trim(), duration || 30, style || "default");
  return { id };
}

export async function importProject(file) {
  await delay(200);
  const id = genId("p");
  const theme = (file && file.name) || "インポート素材";
  const shots = buildShots(theme, 1);
  DB.projects.set(id, {
    id,
    theme,
    created_at: nowIso(),
    status: "ready",
    plan: {
      shots,
      narration_text: "",
      bgm: { file: "calm_01.mp3", gain_db: -14, ducking: true },
      sfx: [],
      subtitle_style: defaultSubtitleStyle(),
    },
    renders: [],
  });
  return { id };
}

export async function putPlan(id, plan) {
  await delay(220);
  const p = DB.projects.get(id);
  if (!p) throw new ApiError("PROJECT_NOT_FOUND", `プロジェクトが見つかりません: ${id}`, 404);
  for (const shot of plan.shots || []) {
    if (shot.trim && shot.trim.start > shot.trim.end) {
      throw new ApiError("INVALID_TRIM", `ショット ${shot.id} のトリム範囲が不正です`, 422);
    }
    if (shot.source_duration != null && shot.trim && shot.trim.end > shot.source_duration + 0.01) {
      throw new ApiError("INVALID_TRIM", `ショット ${shot.id} のトリム範囲が素材長を超えています`, 422);
    }
  }
  const ngWords = ["絶対稼げる", "絶対に稼げる", "100%成功", "100%儲かる", "元本保証", "必ず儲かる"];
  for (const shot of plan.shots || []) {
    if (ngWords.some((w) => (shot.caption || "").includes(w))) {
      throw new ApiError("COMPLIANCE_NG_WORD", `禁止表現が含まれています: 「${shot.caption}」`, 422);
    }
  }
  p.plan = plan;
  return { ok: true };
}

export async function startRender(id) {
  await delay(150);
  const p = DB.projects.get(id);
  if (!p) throw new ApiError("PROJECT_NOT_FOUND", `プロジェクトが見つかりません: ${id}`, 404);
  if (!p.plan || !p.plan.shots || p.plan.shots.every((s) => !s.enabled)) {
    throw new ApiError("NO_ENABLED_SHOTS", "有効なショットがありません。1つ以上有効にしてください", 422);
  }
  p.status = "rendering";
  const job = startRenderJob(id);
  return { job_id: job.id };
}

export async function getBgmAssets() {
  await delay(100);
  return BGM_ASSETS.slice();
}

export async function getSfxAssets() {
  await delay(100);
  return SFX_ASSETS.slice();
}

// SSE 擬似実装。 real-time ではなく setInterval でステージを進める。
export function subscribeJobEvents(jobId, handlers) {
  const job = DB.jobs.get(jobId);
  if (!job) {
    // 直後に無いジョブとして通知（既に完了しキャッシュから消えた等）
    setTimeout(() => handlers.onError && handlers.onError(new ApiError("JOB_NOT_FOUND", "ジョブが見つかりません")), 30);
    return () => {};
  }
  if (job.done) {
    setTimeout(() => handlers.onDone && handlers.onDone({ done: true, ok: true, path: job.resultPath }), 30);
    return () => {};
  }

  let stopped = false;
  const tickMs = qs.get("fastqa") === "1" ? 90 : 420;

  const timer = setInterval(() => {
    if (stopped) return;
    if (job.stepIndex < job.stages.length) {
      const s = job.stages[job.stepIndex];
      handlers.onMessage && handlers.onMessage({ stage: s.stage, progress: s.pct, message: s.message });
      job.stepIndex++;
    } else {
      job.done = true;
      job.onFinish && job.onFinish();
      const project = DB.projects.get(job.projectId);
      const path = job.kind === "render" && project && project.renders.length
        ? project.renders[project.renders.length - 1].path
        : null;
      job.resultPath = path;
      handlers.onMessage && handlers.onMessage({ stage: "done", progress: 100, message: "完了しました" });
      handlers.onDone && handlers.onDone({ done: true, ok: true, path });
      clearInterval(timer);
    }
  }, tickMs);

  return () => { stopped = true; clearInterval(timer); };
}

export function mediaUrl() { return ""; }

export function _debugDB() { return DB; }
