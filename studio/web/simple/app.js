// Reel Studio — かんたんモード（既定UI）。既存の api.js / mock.js をそのまま再利用する。
// 専門用語ゼロ・1画面1アクションを徹底する（詳細は docs/STUDIO-DESIGN.md ではなく、
// 発注時のUI/UX要件「AI初心者・高校生でも迷わない」に準拠）。
import { api, MOCK } from "../api.js";
import { showToast, showApiError } from "../components/toast.js";

const qsParams = new URLSearchParams(location.search);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }
function deepClone(obj) { return obj == null ? obj : JSON.parse(JSON.stringify(obj)); }

// ---------- 中央ステート ----------
const state = {
  screen: "home", // 'home' | 'creating' | 'result' | 'edit'

  projects: [],
  projectsLoading: true,
  projectsError: null,

  assets: { bgm: [], sfx: [], loaded: false },

  theme: "",
  productUrl: "", // 商品リンク（任意）。貼ると商品アフィリエイト動画モードになる
  referenceUrl: "", // 参考動画リンク（任意）。貼るとその動画の構成・テンポを再現した台本になる
  style: "default", // 'default' | 'vertical_hook'
  planTier: "free", // 'free'（むりょうコース） | 'paid'（本番コース）。映像/声/文字起こしの課金をまとめて切替
  estimate: { free: { coins: 0, note: "0円" }, paid: null }, // プランごとの費用見積（paidはAPIで動的取得）
  estimateLoading: false,
  submitting: false,
  submitError: null,

  current: null,
  draftPlan: null,
  dirty: false,
  savingEdit: false,
  editingCaptionId: null,

  job: null, // { phase:'generate'|'render'|'premiere_export', totalPhases, stage, progress, message }
  premiereExport: null, // { path } | null（「Premiereで編集」書き出し完了後の書き出し先パス）
};

let unsubscribeJob = null;
let pollTimer = null;

function setState(patch) { Object.assign(state, patch); render(); }
function stopJobSub() { if (unsubscribeJob) { unsubscribeJob(); unsubscribeJob = null; } }
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ---------- データ読み込み ----------
async function loadProjects() {
  setState({ projectsLoading: true, projectsError: null });
  try {
    const list = await api.listProjects();
    setState({ projectsLoading: false, projects: list });
  } catch (err) {
    setState({ projectsLoading: false, projectsError: err });
  }
}

async function loadAssets() {
  try {
    const [bgm, sfx] = await Promise.all([api.getBgmAssets(), api.getSfxAssets()]);
    setState({ assets: { bgm, sfx, loaded: true } });
  } catch (err) {
    showApiError(err, "素材の読み込みに失敗しました");
    setState({ assets: { bgm: [], sfx: [], loaded: true } });
  }
}

// ---------- ホーム: 入力操作 ----------
// TTP v2 移行後、参考動画URLは必須。空なら送信ボタンを無効化する。
function _isReferenceUrlValid(url) {
  const s = (url || "").trim();
  if (!s) return false;
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch (_e) {
    return false;
  }
}
function syncSubmitButton() {
  const btn = document.getElementById("s-submit");
  if (!btn) return;
  const ok = state.theme.trim() && _isReferenceUrlValid(state.referenceUrl) && !state.submitting;
  btn.disabled = !ok;
}

async function submitCreate() {
  const theme = state.theme.trim();
  if (!theme) return;
  const productUrl = (state.productUrl || "").trim();
  const referenceUrl = (state.referenceUrl || "").trim();
  if (!_isReferenceUrlValid(referenceUrl)) {
    setState({ submitError: { message: "参考動画URLが必要です（http/https の TikTok 等のURLを貼ってください）" } });
    return;
  }
  setState({ submitting: true, submitError: null });
  try {
    const { id } = await api.createProject({
      theme, duration: 30, planTier: state.planTier, style: state.style,
      productUrl: productUrl || undefined, // 空なら送らない（通常モードのまま）
      referenceUrl, // TTP v2: 必須
    });
    setState({ submitting: false });
    await beginGenerate(id);
  } catch (err) {
    setState({ submitting: false, submitError: err });
  }
}

// ---------- 生成〜仕上げの進行（作成中画面を貫通させる） ----------
async function beginGenerate(id) {
  stopJobSub();
  stopPoll();
  let project;
  try {
    project = await api.getProject(id);
  } catch (err) {
    showApiError(err, "読み込みに失敗しました");
    setState({ screen: "home" });
    return;
  }
  setState({
    current: project,
    draftPlan: project.plan ? deepClone(project.plan) : null,
    screen: "creating",
    job: { phase: "generate", totalPhases: 2, stage: "queued", progress: 0, message: "" },
  });

  if (project.status !== "generating") {
    // 過去プロジェクトを開いた場合など: 既に台本ができているので仕上げ(レンダー)へ直行
    await beginRender(id, { totalPhases: project.status === "rendering" ? 1 : 2, resumeIfRendering: project.status === "rendering" });
    return;
  }

  // 契約: 生成ジョブの id はプロジェクト作成レスポンスの{id}と同一（docs/STUDIO-DESIGN.md準拠）
  unsubscribeJob = api.subscribeJobEvents(id, {
    onMessage: (d) => {
      if (!state.job) return;
      setState({ job: { ...state.job, stage: d.stage, progress: d.progress, message: d.message } });
    },
    onDone: async () => {
      stopJobSub();
      try {
        const refreshed = await api.getProject(id);
        setState({ current: refreshed, draftPlan: refreshed.plan ? deepClone(refreshed.plan) : null });
      } catch (err) {
        showApiError(err, "最新状態の取得に失敗しました");
      }
      await beginRender(id, { totalPhases: 2 });
    },
    onError: (err) => {
      showApiError(err, "台本づくりでエラーが発生しました");
      stopJobSub();
      setState({ screen: "home", job: null });
    },
  });
}

async function beginRender(id, { totalPhases = 1, resumeIfRendering = false } = {}) {
  stopJobSub();
  stopPoll();
  setState({
    screen: "creating",
    job: { phase: "render", totalPhases, stage: "queued", progress: resumeIfRendering ? 50 : 0, message: "" },
  });

  if (resumeIfRendering) {
    // 書き出し中に開いた場合: job_idが無いため、2秒おきの再取得で完了を検知する
    pollTimer = setInterval(async () => {
      try {
        const project = await api.getProject(id);
        if (project.status !== "rendering") { stopPoll(); await finishRender(project); }
      } catch (err) { /* 次回のポーリングに任せる */ }
    }, 2000);
    return;
  }

  try {
    const { job_id } = await api.startRender(id);
    unsubscribeJob = api.subscribeJobEvents(job_id, {
      onMessage: (d) => {
        if (!state.job) return;
        setState({ job: { ...state.job, stage: d.stage, progress: d.progress, message: d.message } });
      },
      onDone: async () => {
        stopJobSub();
        try {
          const project = await api.getProject(id);
          await finishRender(project);
        } catch (err) {
          showApiError(err, "最新状態の取得に失敗しました");
          setState({ screen: "edit", job: null });
        }
      },
      onError: (err) => {
        showApiError(err, "仕上げ中にエラーが発生しました");
        stopJobSub();
        setState({ screen: "edit", job: null });
      },
    });
  } catch (err) {
    showApiError(err, "仕上げを開始できませんでした");
    setState({ screen: state.draftPlan ? "edit" : "home", job: null });
  }
}

async function finishRender(project) {
  const failed = project.status === "failed";
  setState({
    current: project,
    draftPlan: project.plan ? deepClone(project.plan) : null,
    dirty: false,
    job: null,
    premiereExport: null,
    screen: failed ? "edit" : "result",
  });
  loadProjects();
  if (failed) showApiError(new Error(project.error || "仕上げに失敗しました"), "仕上げに失敗しました");
}

// ---------- 過去の動画を開く ----------
async function openPast(id) {
  try {
    const project = await api.getProject(id);
    if (project.status === "generating") { await beginGenerate(id); return; }
    if (project.status === "rendering") {
      setState({ current: project, draftPlan: project.plan ? deepClone(project.plan) : null });
      await beginRender(id, { totalPhases: 1, resumeIfRendering: true });
      return;
    }
    setState({ current: project, draftPlan: project.plan ? deepClone(project.plan) : null, dirty: false });
    if (project.status === "ready" && project.renders && project.renders.length) {
      setState({ screen: "result" });
    } else if (project.status === "failed") {
      setState({ screen: "edit" });
      showApiError(new Error(project.error || "前回は失敗しました"), "このプロジェクトは失敗しています");
    } else {
      setState({ screen: "edit" });
    }
  } catch (err) {
    showApiError(err, "開けませんでした");
  }
}

// ---------- かんたん編集 ----------
function updateCaption(shotId, value) {
  const shot = state.draftPlan && state.draftPlan.shots.find((s) => s.id === shotId);
  if (!shot) return;
  shot.caption = value;
  setState({ dirty: true });
}
function startEditCaption(shotId) { setState({ editingCaptionId: shotId }); }
function stopEditCaption() { setState({ editingCaptionId: null }); }

function toggleScene(shotId) {
  const shot = state.draftPlan && state.draftPlan.shots.find((s) => s.id === shotId);
  if (!shot) return;
  shot.enabled = !shot.enabled;
  setState({ dirty: true });
}

function currentMood() {
  const bgm = state.draftPlan && state.draftPlan.bgm;
  if (!bgm || !bgm.file) return null;
  const asset = (state.assets.bgm || []).find((a) => a.file === bgm.file);
  return asset ? asset.mood : null;
}
function setMood(mood) {
  if (!state.draftPlan) return;
  const asset = (state.assets.bgm || []).find((a) => a.mood === mood);
  if (!asset) { showToast({ type: "error", title: "その音楽が見つかりません" }); return; }
  if (!state.draftPlan.bgm) state.draftPlan.bgm = { file: null, gain_db: -14, ducking: true };
  state.draftPlan.bgm.file = asset.file;
  setState({ dirty: true });
}

const VOLUME_MAP = { large: -8, mid: -14, small: -20 };
function currentVolumeKey() {
  const bgm = state.draftPlan && state.draftPlan.bgm;
  const g = bgm ? bgm.gain_db : -14;
  if (g === VOLUME_MAP.large) return "large";
  if (g === VOLUME_MAP.small) return "small";
  return "mid";
}
function setVolume(key) {
  if (!state.draftPlan) return;
  if (!state.draftPlan.bgm) state.draftPlan.bgm = { file: null, gain_db: -14, ducking: true };
  state.draftPlan.bgm.gain_db = VOLUME_MAP[key] != null ? VOLUME_MAP[key] : -14;
  setState({ dirty: true });
}

function sfxIsOn() { return !!(state.draftPlan && state.draftPlan.sfx && state.draftPlan.sfx.length); }
function toggleSfx() {
  if (!state.draftPlan) return;
  if (sfxIsOn()) { state.draftPlan.sfx = []; setState({ dirty: true }); return; }
  const shots = (state.draftPlan.shots || []).filter((s) => s.enabled);
  const sfxAssets = state.assets.sfx || [];
  const sfxAsset = sfxAssets.find((a) => a.file === "whoosh_01.wav") || sfxAssets[0];
  if (!sfxAsset) { showToast({ type: "error", title: "効果音の素材が見つかりません" }); return; }
  let t = 0;
  const list = [];
  shots.forEach((s, i) => {
    if (i > 0) list.push({ file: sfxAsset.file, at_sec: Math.round(t * 10) / 10, gain_db: -8 });
    t += Math.max(0, s.trim.end - s.trim.start);
  });
  state.draftPlan.sfx = list;
  setState({ dirty: true });
}

async function saveDraft() {
  if (!state.current || !state.draftPlan) return false;
  setState({ savingEdit: true });
  try {
    await api.putPlan(state.current.id, state.draftPlan);
    setState({ savingEdit: false, dirty: false, current: { ...state.current, plan: deepClone(state.draftPlan) } });
    return true;
  } catch (err) {
    setState({ savingEdit: false });
    showApiError(err, "保存に失敗しました");
    return false;
  }
}

async function finalizeEdit() {
  if (!state.current) return;
  if (state.dirty) {
    const ok = await saveDraft();
    if (!ok) return;
  }
  await beginRender(state.current.id, { totalPhases: 1 });
}

// ---------- 続きから生成（途中で止まった動画の再開） ----------
function canResume(project) {
  return !!project && project.status === "failed" && (project.unrendered_shot_ids || []).length > 0;
}

async function resumeGenerate() {
  const project = state.current;
  if (!project) return;
  stopJobSub();
  stopPoll();
  setState({
    screen: "creating",
    job: { phase: "render", totalPhases: 1, stage: "queued", progress: 0, message: "続きから作成しています…" },
  });
  try {
    const { job_id } = await api.resumeProject(project.id);
    unsubscribeJob = api.subscribeJobEvents(job_id, {
      onMessage: (d) => {
        if (!state.job) return;
        setState({ job: { ...state.job, stage: d.stage, progress: d.progress, message: d.message } });
      },
      onDone: async () => {
        stopJobSub();
        try {
          const refreshed = await api.getProject(project.id);
          await finishRender(refreshed);
        } catch (err) {
          showApiError(err, "最新状態の取得に失敗しました");
          setState({ screen: "edit", job: null });
        }
      },
      onError: (err) => {
        showApiError(err, "続きからの作成でエラーが発生しました");
        stopJobSub();
        setState({ screen: "edit", job: null });
      },
    });
  } catch (err) {
    showApiError(err, "続きからの作成を開始できませんでした");
    setState({ screen: "edit", job: null });
  }
}

// ---------- Premiereで編集（書き出しパッケージの生成） ----------
async function beginPremiereExport() {
  const project = state.current;
  if (!project) return;
  stopJobSub();
  stopPoll();
  setState({
    screen: "creating",
    premiereExport: null,
    job: { phase: "premiere_export", totalPhases: 1, stage: "queued", progress: 0, message: "" },
  });
  try {
    const { job_id } = await api.premiereExport(project.id);
    unsubscribeJob = api.subscribeJobEvents(job_id, {
      onMessage: (d) => {
        if (!state.job) return;
        setState({ job: { ...state.job, stage: d.stage, progress: d.progress, message: d.message } });
      },
      onDone: (d) => {
        stopJobSub();
        setState({
          screen: "result",
          job: null,
          premiereExport: d.ok && d.path
            ? { path: d.path, auto: !!d.auto, message: d.message || "" }
            : null,
        });
        if (!d.ok) showApiError(new Error("Premiereへの書き出しに失敗しました"), "書き出しに失敗しました");
      },
      onError: (err) => {
        showApiError(err, "Premiereへの書き出し中にエラーが発生しました");
        stopJobSub();
        setState({ screen: "result", job: null });
      },
    });
  } catch (err) {
    showApiError(err, "Premiereへの書き出しを開始できませんでした");
    setState({ screen: "result", job: null });
  }
}

// ---------- できあがり画面の操作 ----------
function goEdit() { setState({ screen: "edit", editingCaptionId: null }); }
function goHomeFresh() {
  stopJobSub(); stopPoll();
  setState({
    screen: "home", current: null, draftPlan: null, job: null, premiereExport: null,
    theme: "", productUrl: "", referenceUrl: "", submitError: null,
  });
  loadProjects();
}
function saveLink() {
  const project = state.current;
  if (!project || !project.renders || !project.renders.length) return "";
  return api.mediaUrl("raw", project.renders[project.renders.length - 1].path);
}

// ---------- 共通パーツ ----------
function topbarHtml() {
  return `<div class="s-topbar"><span class="logo-dot" aria-hidden="true"></span><span>Reel Studio かんたんモード</span></div>`;
}
// コース＋消費コインの小さなバッジ。plan_tier と billing（サーバ/モック記録）から作る。
// tier 未保存の旧プロジェクトは backend から後方互換で推定する。
function courseChipHtml(tier, coinsActual, coinsEstimated) {
  const isFree = tier !== "paid";
  const label = isFree ? "むりょうコース" : "本番コース";
  let coinText;
  if (isFree) coinText = "0円（無料）";
  else if (coinsActual != null) coinText = `${coinsActual} コイン消費`;
  else if (coinsEstimated != null) coinText = `見積 約${coinsEstimated} コイン`;
  else coinText = "コース: 有料";
  return `<div class="s-course-chip s-course-chip--${isFree ? "free" : "paid"}">
    <span class="s-course-chip__label">${escapeHtml(label)}</span>
    <span class="s-course-chip__coins">${escapeHtml(coinText)}</span>
  </div>`;
}
function courseChipForProject(project) {
  if (!project) return "";
  const billing = project.billing || {};
  // 明示の plan_tier / billing.plan_tier が最優先。無い旧プロジェクトは生成バックエンドから推定する
  // （mock→free / higgsfield・cloudapi→paid）。import 等コース概念の無いプロジェクトは表示しない
  // （アップロードしただけの動画を「本番コース(有料)」と誤表示しないため）。
  const BACKEND_TIER = { mock: "free", higgsfield: "paid", cloudapi: "paid" };
  const tier = project.plan_tier || billing.plan_tier || BACKEND_TIER[project.backend || ""];
  if (!tier) return "";
  return courseChipHtml(tier, billing.coins_actual, billing.coins_estimated);
}
function proModeHref() {
  const p = new URLSearchParams(location.search);
  p.set("pro", "1");
  return "/?" + p.toString();
}
function footerHtml() {
  return `<div class="s-footer"><a href="${proModeHref()}">プロ向け画面</a></div>`;
}

// 台本メタ（どのAIで作られたか / AI接続失敗で自動テンプレになったか）の小さな表示。
// プロジェクト詳細（かんたん編集画面）向け。専門用語を避け、失敗時のみ目立たせる。
function directorMetaHtml(project) {
  const director = project && project.director;
  if (!director) return "";
  if (director.source === "ai") {
    return `<div class="s-director-note">台本AI: ${escapeHtml(director.model_used || "不明")}</div>`;
  }
  return `<div class="s-director-note s-director-note--warn">
    <strong>注意:</strong> 台本はAI接続に失敗したため、自動テンプレで作成されています
  </div>`;
}

// 音声AI（Fish Audio）の使用有無 / フォールバック時の注意表示。専門用語を避け、
// フォールバック理由は日本語の一言に変換する（project["tts"] = pipeline/tts.py の
// synthesize() 返り値そのもの: backend/duration_sec/is_silent/requested_backend/fallback_reason）。
const TTS_FALLBACK_REASON_LABELS = {
  api_key_missing: "APIキーが未設定です",
  http_error_401: "APIキーが正しくありません",
  http_error_403: "APIの利用が許可されていません",
  retry_exhausted: "サーバーが混み合っています",
  save_failed: "音声の保存に失敗しました",
  ffmpeg_convert_failed: "音声の変換に失敗しました",
  say_failed: "音声合成に失敗しました",
  voice_unavailable: "この声が使えません",
  unknown_error: "不明なエラーが発生しました",
};
function ttsMetaHtml(project) {
  const ttsMeta = project && project.tts;
  if (!ttsMeta) return "";
  if (ttsMeta.requested_backend === "fish_audio" && ttsMeta.fallback_reason) {
    const reason = TTS_FALLBACK_REASON_LABELS[ttsMeta.fallback_reason] || ttsMeta.fallback_reason;
    return `<div class="s-director-note s-director-note--warn">
      <strong>注意:</strong> 音声: macOS標準に切替（理由: ${escapeHtml(reason)}）
    </div>`;
  }
  if (ttsMeta.backend === "fish_audio") {
    return `<div class="s-director-note">音声AI: Fish Audio</div>`;
  }
  return "";
}

// 商品アフィリエイト動画モードで取得した商品画像のサムネイル行。
// project["product"] は {"name","url","images":[{"path","source","width","height"}],"warnings"}。
function productThumbsHtml(project) {
  const product = project && project.product;
  if (!product || !product.images || !product.images.length) return "";
  return `
    <div class="s-card">
      <div class="s-section-title">使った商品画像</div>
      ${product.name ? `<div class="s-section-sub">${escapeHtml(product.name)}</div>` : ""}
      <div class="s-product-thumbs">
        ${product.images.map((img) => `
          <div class="s-product-thumb">
            <img src="${escapeHtml(api.mediaUrl("raw", img.path))}" alt="商品画像" loading="lazy">
          </div>`).join("")}
      </div>
      ${(product.warnings || []).length
        ? `<div class="s-hint" style="margin-top:10px;">${escapeHtml(product.warnings.join(" / "))}</div>`
        : ""}
    </div>`;
}

// 参考動画リンクの解析結果の小さな表示。project["reference"] は
// {"url","ok":true,"source","cached","duration_sec","beats_count","warnings"} または
// {"url","ok":false,"error"} または null（参考動画リンク未指定）。
// 解析に失敗していても生成自体は通常の台本で続行済み（fail-open）のため、注意表示に留める。
function referenceInfoHtml(project) {
  const reference = project && project.reference;
  if (!reference) return "";
  if (reference.ok) {
    const note = reference.cached ? "構成を再現・キャッシュ利用" : "構成を再現";
    return `<div class="s-director-note">参考動画: ${escapeHtml(reference.url)}（${escapeHtml(note)}）</div>`;
  }
  return `<div class="s-director-note s-director-note--warn">
    <strong>注意:</strong> 参考動画を解析できなかったため、通常の台本で作成しました（${escapeHtml(reference.error || "")}）
  </div>`;
}

// 失敗の原因履歴（最新2件）。project["error"]は最新の失敗理由で上書きされるが、
// error_historyはこれまでの失敗をすべて追記で残しているため、経緯を確認できる。
function errorHistoryHtml(project) {
  const history = (project && project.error_history) || [];
  if (!history.length) return "";
  const recent = history.slice(-2).slice().reverse();
  return `
    <div class="s-error-history">
      <div class="s-error-history__title">これまでのエラー</div>
      ${recent.map((h) => `<div class="s-error-history__item">${escapeHtml(h.message)}</div>`).join("")}
    </div>`;
}

const HIST_THUMB_ICON = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 4v5M16 4v5"/></svg>`;

const HIST_PILL = {
  draft: ["下書き", "draft"],
  generating: ["生成中", "generating"],
  ready: ["完成", "ready"],
  rendering: ["仕上げ中", "rendering"],
  failed: ["失敗", "failed"],
};

function renderHistoryList() {
  if (state.projectsLoading) return `<div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div>`;
  if (state.projectsError) return `<div class="s-error-banner">一覧を読み込めませんでした</div>`;
  if (!state.projects.length) return `<div class="s-section-sub">まだ動画がありません。上のフォームから最初の1本を作ってみましょう。</div>`;
  return `<div class="s-history-list">${state.projects.map((p) => {
    const label = HIST_PILL[p.status] || ["-", p.status];
    return `
    <button type="button" class="s-history-card" data-open-project="${escapeHtml(p.id)}">
      <div class="s-history-card__thumb" aria-hidden="true">${HIST_THUMB_ICON}</div>
      <div class="s-history-card__body">
        <div class="s-history-card__title">${escapeHtml(p.theme)}</div>
        <div class="s-history-card__meta"><span class="s-pill s-pill--${escapeHtml(label[1])}">${escapeHtml(label[0])}</span></div>
      </div>
    </button>`;
  }).join("")}</div>`;
}

function styleCardHtml(value, title, desc) {
  const pressed = state.style === value;
  const art = value === "default"
    ? `<svg width="40" height="28" viewBox="0 0 40 28" fill="none"><rect x="2" y="4" width="36" height="4" rx="2" fill="currentColor" opacity="0.85"/><rect x="2" y="12" width="26" height="4" rx="2" fill="currentColor" opacity="0.55"/><rect x="2" y="20" width="30" height="4" rx="2" fill="currentColor" opacity="0.35"/></svg>`
    : `<svg width="28" height="28" viewBox="0 0 28 28" fill="none"><rect x="2" y="2" width="6" height="24" rx="3" fill="currentColor" opacity="0.85"/><rect x="11" y="2" width="6" height="18" rx="3" fill="currentColor" opacity="0.55"/><rect x="20" y="2" width="6" height="24" rx="3" fill="currentColor" opacity="0.35"/></svg>`;
  return `
    <button type="button" class="s-style-card" data-style="${value}" aria-pressed="${pressed}">
      <div class="s-style-card__art" aria-hidden="true">${art}</div>
      <div class="s-style-card__title">${escapeHtml(title)}</div>
      <div class="s-style-card__desc">${escapeHtml(desc)}</div>
    </button>`;
}
// ---------- プラン（ワンセット）2択カード ----------
// free=むりょうコース（青）/ paid=本番コース（琥珀）。1枚に「費用・何ができるか」を近接配置。
const ICON_FLASK = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6"/><path d="M10 3v5.3L4.9 17a2 2 0 0 0 1.7 3h10.8a2 2 0 0 0 1.7-3L14 8.3V3"/><path d="M7.6 14h8.8"/></svg>`;
const ICON_GEM = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12l4 6-10 12L2 9z"/><path d="M2 9h20M9 3 6 9l6 12 6-12-3-6M9 3l3 6 3-6"/></svg>`;
const ICON_CHECK = `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5 6.2 11.7 13 4.5"/></svg>`;

// paid の見積が未取得なら API を叩いて取得する（実生成はしない・0円保証のfree側は叩かない）。
async function ensurePaidEstimate() {
  if (state.estimate.paid || state.estimateLoading) return;
  setState({ estimateLoading: true });
  try {
    const est = await api.estimate({ planTier: "paid", duration: 30 });
    setState({ estimateLoading: false, estimate: { ...state.estimate, paid: est } });
  } catch (_e) {
    setState({ estimateLoading: false, estimate: { ...state.estimate, paid: { coins: null, note: "見積を取得できませんでした" } } });
  }
}

// この動画にかかる費用の1行表示。free=「0円」/ paid=「約◯コイン」（取得前は「計算中…」）。
function planCostLabel(tier) {
  if (tier === "free") return "0円";
  const est = state.estimate.paid;
  if (state.estimateLoading && !est) return "計算中…";
  if (!est) return "約 … コイン";
  if (est.coins == null) return "見積エラー";
  return `約 ${est.coins} コイン`;
}

function planCardHtml(tier, iconSvg, badge, title, sub, features) {
  const pressed = state.planTier === tier;
  return `
    <button type="button" class="s-plan-card s-plan-card--${tier}" data-plan="${tier}" aria-pressed="${pressed}">
      <div class="s-plan-card__head">
        <span class="s-plan-card__icon" aria-hidden="true">${iconSvg}</span>
        <span class="s-plan-card__badge">${escapeHtml(badge)}</span>
      </div>
      <div class="s-plan-card__title">${escapeHtml(title)}</div>
      <div class="s-plan-card__sub">${escapeHtml(sub)}</div>
      <div class="s-plan-card__cost">
        <span class="s-plan-card__cost-label">この動画の費用</span>
        <span class="s-plan-card__cost-value">${escapeHtml(planCostLabel(tier))}</span>
      </div>
      <ul class="s-plan-card__features">
        ${features.map((f) => `<li><span class="s-plan-card__check" aria-hidden="true">${ICON_CHECK}</span>${escapeHtml(f)}</li>`).join("")}
      </ul>
    </button>`;
}

function planCardsHtml() {
  return `
    <div class="s-plan-grid" role="group" aria-label="プランを選ぶ">
      ${planCardHtml("free", ICON_FLASK, "むりょう", "おためしコース", "まずは無料でお試し",
        ["映像: 練習用の仮映像", "声: パソコン内蔵の音声", "料金: ずっと0円"])}
      ${planCardHtml("paid", ICON_GEM, "本番", "本番コース", "AI映像と自然な声で仕上げる",
        ["映像: AIが生成する本物の映像", "声: 自然なAIナレーション", "料金: コインを消費（下に表示）"])}
    </div>`;
}

// ---------- 画面: ホーム ----------
function renderHome() {
  const el = document.getElementById("simple-app");
  const chips = ["朝5分のストレッチ習慣", "スマホ副業のはじめかた", "毎日続く片付け術"];
  el.innerHTML = `
    ${topbarHtml()}
    <div class="s-hero">
      <h1>ショート動画を自動作成</h1>
      <p>テーマを入力するだけで、台本と映像をAIが自動生成します。</p>
    </div>
    <div class="s-card">
      <div class="s-section-title">テーマを入力</div>
      <div class="s-input-wrap">
        <textarea class="s-textarea" id="s-theme" placeholder="例: 朝5分のストレッチ習慣" maxlength="80">${escapeHtml(state.theme)}</textarea>
        <div class="s-chips">
          ${chips.map((c) => `<button type="button" class="s-chip" data-chip="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join("")}
        </div>
      </div>

      <div class="s-section-title">商品リンク（任意）</div>
      <div class="s-input-wrap">
        <input type="url" class="s-url-input" id="s-product-url" placeholder="https://example.com/product/123" maxlength="500" value="${escapeHtml(state.productUrl)}">
        <div class="s-hint" style="margin-top:8px;">貼ると商品画像を自動で取得して動画の主役にします。assets/products/inbox に入れた画像も使われます</div>
      </div>

      <div class="s-section-title">参考動画リンク（必須）</div>
      <div class="s-input-wrap">
        <input type="url" class="s-url-input" id="s-reference-url" placeholder="https://www.tiktok.com/@example/video/123" maxlength="500" required value="${escapeHtml(state.referenceUrl)}">
        <div class="s-hint" style="margin-top:8px;">TikTokのURLを貼ると、その動画の構成・テンポを再現した台本になります（文言はコピーしません）。TTP v2 では参考動画URLが必須です</div>
      </div>

      <div class="s-section-title">見た目のスタイル</div>
      <div class="s-style-grid" role="group" aria-label="スタイル選択">
        ${styleCardHtml("default", "ふつう", "読みやすい横書きテロップ")}
        ${styleCardHtml("vertical_hook", "バズ風", "縦書きテロップ・速いカット")}
      </div>

      <div class="s-section-title" style="margin-top:16px;">プランを選ぶ</div>
      ${planCardsHtml()}
      <div class="s-hint">「むりょう」はずっと0円でお試しできます。「本番」はAIが本物の映像と声を作り、コインを消費します（かかるコイン数は上に表示されます）。</div>

      ${state.submitError ? `<div class="s-error-banner" role="alert">${escapeHtml(state.submitError.message)}</div>` : ""}

      <button type="button" class="s-cta" id="s-submit" style="margin-top:18px;" ${(!state.theme.trim() || !_isReferenceUrlValid(state.referenceUrl) || state.submitting) ? "disabled" : ""}>
        ${state.submitting ? "準備しています…" : "動画を作成"}
      </button>
    </div>

    <div class="s-card">
      <div class="s-section-title">これまでの動画</div>
      ${renderHistoryList()}
    </div>

    ${footerHtml()}
  `;

  const themeInput = el.querySelector("#s-theme");
  themeInput.addEventListener("input", (e) => { state.theme = e.target.value; syncSubmitButton(); });
  const productUrlInput = el.querySelector("#s-product-url");
  productUrlInput.addEventListener("input", (e) => { state.productUrl = e.target.value; });
  const referenceUrlInput = el.querySelector("#s-reference-url");
  referenceUrlInput.addEventListener("input", (e) => { state.referenceUrl = e.target.value; syncSubmitButton(); });
  el.querySelectorAll("[data-chip]").forEach((btn) => btn.addEventListener("click", () => setState({ theme: btn.dataset.chip })));
  el.querySelectorAll("[data-style]").forEach((btn) => btn.addEventListener("click", () => setState({ style: btn.dataset.style })));
  el.querySelectorAll("[data-plan]").forEach((btn) => btn.addEventListener("click", () => {
    const tier = btn.dataset.plan;
    setState({ planTier: tier });
    if (tier === "paid") ensurePaidEstimate(); // 選んだ瞬間に見積を取得して費用を動的表示
  }));
  el.querySelector("#s-submit").addEventListener("click", submitCreate);
  el.querySelectorAll("[data-open-project]").forEach((c) => c.addEventListener("click", () => openPast(c.dataset.openProject)));
}

// ---------- 画面: 作成中 ----------
const STEP_DEFS_FULL = [
  { label: "台本を作成中" },
  { label: "映像を生成中" },
  { label: "仕上げ中" },
];
const STEP_DEFS_RENDER_ONLY = [
  { label: "映像を生成中" },
  { label: "仕上げ中" },
];
const STEP_DEFS_PREMIERE = [
  { label: "Premiere用ファイルを準備中" },
];
const STEP_ICON_DONE = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5 6.2 11.7 13 4.5"/></svg>`;

function overallProgress(job) {
  if (!job) return 0;
  if (job.totalPhases === 2) return job.phase === "generate" ? job.progress * 0.5 : 50 + job.progress * 0.5;
  return job.progress;
}
function activeStepIndex(job, steps) {
  const pct = overallProgress(job);
  const per = 100 / steps.length;
  return clamp(Math.floor(pct / per), 0, steps.length - 1);
}
function encourageText(pct) {
  if (pct >= 95) return "まもなく完成します";
  if (pct >= 70) return "あと少しです";
  if (pct >= 35) return "順調に進んでいます";
  return "準備しています";
}
function stepIconHtml(i, activeIdx) {
  if (i < activeIdx) return STEP_ICON_DONE;
  if (i === activeIdx) return `<span class="s-step__spinner" aria-hidden="true"></span>`;
  return `<span>${i + 1}</span>`;
}

function renderCreating() {
  const el = document.getElementById("simple-app");
  const job = state.job || { phase: "generate", totalPhases: 2, progress: 0, message: "" };
  const steps = job.phase === "premiere_export"
    ? STEP_DEFS_PREMIERE
    : (job.totalPhases === 2 ? STEP_DEFS_FULL : STEP_DEFS_RENDER_ONLY);
  const pct = Math.round(clamp(overallProgress(job), 0, 100));
  const activeIdx = activeStepIndex(job, steps);
  const theme = state.current ? state.current.theme : state.theme;

  el.innerHTML = `
    ${topbarHtml()}
    <div class="s-card s-creating">
      <h2>作成しています</h2>
      <div class="s-creating__theme">「${escapeHtml(theme || "")}」</div>
      ${state.current ? courseChipForProject(state.current) : courseChipHtml(state.planTier, null, state.planTier === "paid" && state.estimate.paid ? state.estimate.paid.coins : (state.planTier === "free" ? 0 : null))}
      <div class="s-steps">
        ${steps.map((s, i) => `
          <div class="s-step ${i < activeIdx ? "is-done" : ""} ${i === activeIdx ? "is-active" : ""}">
            <span class="s-step__icon" aria-hidden="true">${stepIconHtml(i, activeIdx)}</span>
            <span>${escapeHtml(s.label)}</span>
          </div>
        `).join("")}
      </div>
      <div class="s-progress-pct">${pct}%</div>
      <div class="s-progress-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
        <div class="s-progress-bar__fill" style="width:${pct}%"></div>
      </div>
      <div class="s-encourage">${escapeHtml(encourageText(pct))}</div>
    </div>
    ${footerHtml()}
  `;
}

// ---------- 画面: できあがり ----------
function premiereExportBannerHtml() {
  if (!state.premiereExport) return "";
  const { path, auto, message } = state.premiereExport;
  // auto=true: Premiereへの自動インポートまで完了（Phase B）。
  // auto=false: パッケージの書き出しのみ（Phase A・READMEから手動で読み込む）。
  const title = auto ? "Premiereに組み上げました" : "書き出し先";
  const hint = auto
    ? "Premiereでプロジェクトが開かれています。字幕トラックが自動作成されなかった場合は、captions.srtをタイムラインへドラッグしてください。"
    : "開き方はフォルダ内のREADME_import.mdを見てください";
  return `
    <div class="s-premiere-banner">
      <div class="s-premiere-banner__title">${escapeHtml(title)}</div>
      <div class="s-premiere-banner__path">${escapeHtml(path)}</div>
      <div class="s-premiere-banner__hint">${escapeHtml(message || hint)}</div>
    </div>`;
}

function renderResult() {
  const el = document.getElementById("simple-app");
  const project = state.current;
  const link = saveLink();
  el.innerHTML = `
    ${topbarHtml()}
    ${directorMetaHtml(project)}
    ${ttsMetaHtml(project)}
    ${referenceInfoHtml(project)}
    ${productThumbsHtml(project)}
    <div class="s-card">
      <div class="s-section-title">動画が完成しました</div>
      <div class="s-section-sub">「${escapeHtml(project ? project.theme : "")}」</div>
      ${courseChipForProject(project)}
      ${project && project.telop_style && project.telop_style.display_name
        ? `<div class="s-section-sub">テロップ: ${escapeHtml(project.telop_style.display_name)}</div>`
        : ""}
      <div class="s-video-wrap">
        <div class="preview-frame">
          ${link
            ? `<video src="${escapeHtml(link)}" controls playsinline preload="metadata" aria-label="完成した動画"></video>`
            : `<div class="preview-frame__placeholder">${MOCK ? "デモ素材モードのため、実際の動画ファイルはありません。" : "動画を読み込めませんでした。"}</div>`}
        </div>
      </div>
      <div class="s-result-actions">
        ${link
          ? `<a class="s-cta" href="${escapeHtml(link)}" download>保存</a>`
          : `<button type="button" class="s-cta" disabled>保存</button>`}
        <div class="s-result-actions-row">
          <button type="button" class="s-cta-secondary" id="s-goedit">編集する</button>
          <button type="button" class="s-cta-secondary" id="s-goagain">もう一度作成</button>
        </div>
        <button type="button" class="s-cta-secondary" id="s-premiere-export">Premiereで編集</button>
        ${premiereExportBannerHtml()}
      </div>
    </div>
    ${footerHtml()}
  `;
  el.querySelector("#s-goedit").addEventListener("click", goEdit);
  el.querySelector("#s-goagain").addEventListener("click", goHomeFresh);
  const premiereBtn = el.querySelector("#s-premiere-export");
  if (premiereBtn) premiereBtn.addEventListener("click", beginPremiereExport);
}

// ---------- 画面: かんたん編集 ----------
function captionItemHtml(shot, i) {
  if (state.editingCaptionId === shot.id) {
    return `
      <div class="s-caption-item" data-caption-id="${shot.id}">
        <div class="s-caption-item__num">${i + 1}</div>
        <textarea maxlength="120" aria-label="シーン${i + 1}のテロップ">${escapeHtml(shot.caption)}</textarea>
      </div>`;
  }
  return `
    <div class="s-caption-item" data-caption-id="${shot.id}" role="button" tabindex="0">
      <div class="s-caption-item__num">${i + 1}</div>
      <div class="s-caption-item__text">${shot.caption ? escapeHtml(shot.caption) : "(テロップなし・タップして入力)"}</div>
    </div>`;
}
function sceneItemHtml(shot, i) {
  return `
    <div class="s-scene-item ${shot.enabled ? "" : "is-off"}">
      <div class="s-scene-item__thumb" aria-hidden="true"></div>
      <div class="s-scene-item__label">シーン${i + 1}: ${escapeHtml(shot.caption || "(無題)")}</div>
      <button type="button" class="toggle" role="switch" aria-checked="${!!shot.enabled}" aria-label="シーン${i + 1}を使う" data-scene-toggle="${shot.id}"></button>
    </div>`;
}
function musicMoodBtn(value, label, current) {
  return `<button type="button" class="s-music-btn" data-mood="${value}" aria-pressed="${current === value}">${escapeHtml(label)}</button>`;
}
function volumeBtn(value, label, current) {
  return `<button type="button" class="s-volume-btn" data-volume="${value}" aria-pressed="${current === value}">${escapeHtml(label)}</button>`;
}

function renderEdit() {
  const el = document.getElementById("simple-app");
  const plan = state.draftPlan;
  const shots = (plan && plan.shots) || [];
  const mood = currentMood();
  const volumeKey = currentVolumeKey();
  const sfxOn = sfxIsOn();
  const canFinalize = shots.some((s) => s.enabled);

  const resumable = canResume(state.current);

  el.innerHTML = `
    ${topbarHtml()}
    ${directorMetaHtml(state.current)}
    ${ttsMetaHtml(state.current)}
    ${referenceInfoHtml(state.current)}
    ${productThumbsHtml(state.current)}
    ${state.current && state.current.status === "failed" ? `
      <div class="s-card">
        <div class="s-section-title">前回は完成しませんでした</div>
        <div class="s-section-sub">${escapeHtml((state.current && state.current.error) || "")}</div>
        ${errorHistoryHtml(state.current)}
        ${resumable ? `<button type="button" class="s-cta-secondary" id="s-resume" style="margin-top:10px;">続きから作成する</button>` : ""}
      </div>` : ""}
    <div class="s-card">
      <div class="s-section-title">① テロップ</div>
      <div class="s-section-sub">タップして編集できます</div>
      <div class="s-hint">※テロップの文言と音声のナレーションは別々に管理されています（音声の読み上げ内容を変えたい場合は最初から作り直してください）</div>
      <div class="s-edit-list">
        ${shots.map((s, i) => captionItemHtml(s, i)).join("")}
      </div>
    </div>

    <div class="s-card">
      <div class="s-section-title">② シーン</div>
      <div class="s-section-sub">オフにしたシーンはカットされます</div>
      <div class="s-edit-list">
        ${shots.map((s, i) => sceneItemHtml(s, i)).join("")}
      </div>
    </div>

    <div class="s-card">
      <div class="s-section-title">③ 音楽</div>
      <div class="s-music-grid" role="group" aria-label="音楽の雰囲気">
        ${musicMoodBtn("upbeat", "アップビート", mood)}
        ${musicMoodBtn("calm", "落ち着いた", mood)}
        ${musicMoodBtn("emotional", "エモーショナル", mood)}
      </div>
      <div class="s-section-sub">音の大きさ</div>
      <div class="s-volume-grid" role="group" aria-label="音の大きさ">
        ${volumeBtn("large", "大", volumeKey)}
        ${volumeBtn("mid", "中", volumeKey)}
        ${volumeBtn("small", "小", volumeKey)}
      </div>
    </div>

    <div class="s-card">
      <div class="s-section-title">④ 効果音</div>
      <div class="s-sfx-row">
        <span class="s-sfx-row__label">場面が変わるときに音を鳴らす</span>
        <button type="button" class="toggle" id="s-sfx-toggle" role="switch" aria-checked="${sfxOn}" aria-label="効果音"></button>
      </div>
    </div>

    ${state.dirty ? `<div class="s-hint">まだ保存していません。「この内容で仕上げる」を押すと反映されます。</div>` : ""}
    <button type="button" class="s-cta s-edit-submit" id="s-finalize" ${(!canFinalize || state.savingEdit) ? "disabled" : ""}>
      ${state.savingEdit ? "保存しています…" : "この内容で仕上げる"}
    </button>
    ${footerHtml()}
  `;

  shots.forEach((s) => {
    const item = el.querySelector(`[data-caption-id="${s.id}"]`);
    if (!item) return;
    if (state.editingCaptionId === s.id) {
      const ta = item.querySelector("textarea");
      ta.focus();
      ta.selectionStart = ta.value.length;
      ta.addEventListener("blur", () => { updateCaption(s.id, ta.value); stopEditCaption(); });
    } else {
      const activate = () => startEditCaption(s.id);
      item.addEventListener("click", activate);
      item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } });
    }
  });

  el.querySelectorAll("[data-scene-toggle]").forEach((btn) => btn.addEventListener("click", () => toggleScene(btn.dataset.sceneToggle)));
  el.querySelectorAll("[data-mood]").forEach((btn) => btn.addEventListener("click", () => setMood(btn.dataset.mood)));
  el.querySelectorAll("[data-volume]").forEach((btn) => btn.addEventListener("click", () => setVolume(btn.dataset.volume)));
  el.querySelector("#s-sfx-toggle").addEventListener("click", toggleSfx);
  el.querySelector("#s-finalize").addEventListener("click", finalizeEdit);
  const resumeBtn = el.querySelector("#s-resume");
  if (resumeBtn) resumeBtn.addEventListener("click", resumeGenerate);
}

// ---------- 描画ディスパッチ ----------
function render() {
  switch (state.screen) {
    case "creating": renderCreating(); break;
    case "result": renderResult(); break;
    case "edit": renderEdit(); break;
    case "home":
    default: renderHome(); break;
  }
}

// ---------- 初期化（?open=<id> でQA時の状態を固定） ----------
async function init() {
  render();
  await Promise.all([loadProjects(), loadAssets()]);

  const openId = qsParams.get("open");
  if (openId) await openPast(openId);
  render();
}

init();
