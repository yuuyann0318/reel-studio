// Reel Studio — アプリコントローラ（中央ストア + 描画ディスパッチ）
import { api, MOCK } from "./api.js";
import { showToast, showApiError } from "./components/toast.js";
import { renderProjectList } from "./components/projectList.js";
import { renderAssetBrowser } from "./components/assetBrowser.js";
import { renderPreview } from "./components/preview.js";
import { renderInspector } from "./components/inspector.js";
import { renderTimeline } from "./components/timeline.js";

const qsParams = new URLSearchParams(location.search);

const STATUS_LABEL = {
  draft: "下書き",
  generating: "生成中",
  ready: "準備完了",
  rendering: "書き出し中",
  failed: "失敗",
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }
function deepClone(obj) { return obj == null ? obj : JSON.parse(JSON.stringify(obj)); }

// ---------- 中央ステート ----------
const state = {
  projects: [],
  projectsLoading: true,
  projectsError: null,

  assets: { bgm: [], sfx: [], loading: true, error: null, tab: "bgm", playing: null },

  current: null,
  currentLoading: false,
  currentError: null,
  draftPlan: null,
  dirty: false,
  saving: false,
  selectedShotId: null,

  job: null, // { id, kind:'generate'|'render', projectId, stage, progress, message, done }

  showCreateForm: false,
  createForm: { theme: "", duration: 30, backend: "mock", style: "default" },
  createSubmitting: false,
  createError: null,

  previewMode: "shot", // 'shot' | 'final'
  fakePlaying: false,
  highlightedSfxIndex: null,
};

let unsubscribeJob = null;
let sharedAudio = null;
let assetAudioTimer = null;
let mockAudioNoticeShown = false;

function stopJobSubscription() {
  if (unsubscribeJob) { unsubscribeJob(); unsubscribeJob = null; }
}
function getSharedAudio() {
  if (!sharedAudio) { sharedAudio = new Audio(); sharedAudio.preload = "none"; }
  return sharedAudio;
}

function setState(patch) {
  Object.assign(state, patch);
  render();
}

// ---------- データ読み込み ----------
async function loadProjects() {
  setState({ projectsLoading: true, projectsError: null });
  try {
    const list = await api.listProjects();
    setState({ projects: list, projectsLoading: false });
  } catch (err) {
    setState({ projectsLoading: false, projectsError: err });
  }
}

async function loadAssets() {
  setState({ assets: { ...state.assets, loading: true, error: null } });
  try {
    const [bgm, sfx] = await Promise.all([api.getBgmAssets(), api.getSfxAssets()]);
    setState({ assets: { ...state.assets, bgm, sfx, loading: false } });
  } catch (err) {
    setState({ assets: { ...state.assets, loading: false, error: err } });
  }
}

async function openProject(id) {
  stopJobSubscription();
  setState({
    currentLoading: true, currentError: null, current: null, draftPlan: null,
    selectedShotId: null, previewMode: "shot", dirty: false, job: null,
  });
  try {
    const project = await api.getProject(id);
    const firstShotId = project.plan && project.plan.shots.length ? project.plan.shots[0].id : null;
    setState({
      current: project,
      currentLoading: false,
      draftPlan: project.plan ? deepClone(project.plan) : null,
      selectedShotId: firstShotId,
    });
    if (project.status === "generating") subscribeToJob(project.id, "generate");
    else if (project.status === "rendering") {
      // レンダリング中に開いた場合: job_id が無いため、完了はプロジェクト再取得で検知する
      showToast({ type: "info", title: "書き出し中のプロジェクトです", message: "完了すると自動的に反映されます" });
    }
  } catch (err) {
    setState({ currentLoading: false, currentError: err });
    showApiError(err, "プロジェクトを開けませんでした");
  }
}

function reopenCurrent() { if (state.current) openProject(state.current.id); }

function subscribeToJob(jobId, kind) {
  stopJobSubscription();
  setState({ job: { id: jobId, kind, projectId: state.current.id, stage: "queued", progress: 0, message: "", done: false } });
  unsubscribeJob = api.subscribeJobEvents(jobId, {
    onMessage: (data) => {
      if (!state.job) return;
      setState({ job: { ...state.job, stage: data.stage, progress: data.progress, message: data.message } });
    },
    onDone: async () => {
      const finishedKind = state.job ? state.job.kind : kind;
      stopJobSubscription();
      showToast({ type: "success", title: finishedKind === "render" ? "書き出しが完了しました" : "生成が完了しました" });
      await refreshCurrentAfterJob(finishedKind);
    },
    onError: (err) => {
      showApiError(err, "進捗の受信に失敗しました");
      stopJobSubscription();
      setState({ job: null });
    },
  });
}

async function refreshCurrentAfterJob(kind) {
  const id = state.current && state.current.id;
  if (!id) return;
  try {
    const project = await api.getProject(id);
    const keepShot = state.selectedShotId && project.plan && project.plan.shots.some((s) => s.id === state.selectedShotId);
    setState({
      current: project,
      draftPlan: project.plan ? deepClone(project.plan) : null,
      dirty: false,
      selectedShotId: keepShot ? state.selectedShotId : project.plan && project.plan.shots[0] ? project.plan.shots[0].id : null,
      previewMode: kind === "render" ? "final" : "shot",
      job: null,
      projects: state.projects.map((p) => (p.id === id ? { ...p, status: project.status } : p)),
    });
  } catch (err) {
    showApiError(err, "最新状態の取得に失敗しました");
  }
}

function closeProject() {
  stopJobSubscription();
  setState({ current: null, draftPlan: null, selectedShotId: null, currentError: null, previewMode: "shot", job: null });
}

// ---------- 新規作成フォーム ----------
function toggleCreateForm(force) {
  setState({ showCreateForm: typeof force === "boolean" ? force : !state.showCreateForm, createError: null });
}

async function submitCreateProject({ theme, duration, backend, style }) {
  setState({ createSubmitting: true, createError: null });
  try {
    const { id } = await api.createProject({ theme, duration, backend, style });
    setState({ createSubmitting: false, showCreateForm: false, createForm: { theme: "", duration: 30, backend: "mock", style: "default" } });
    await loadProjects();
    await openProject(id); // status: generating のはずなので openProject 内で自動的に SSE 購読される
  } catch (err) {
    setState({ createSubmitting: false, createError: err });
  }
}

// ---------- ショット / プラン編集 ----------
function selectShot(id) { setState({ selectedShotId: id, previewMode: "shot" }); }

function moveShotSelection(delta) {
  if (!state.draftPlan) return;
  const shots = state.draftPlan.shots;
  if (!shots || !shots.length) return;
  const idx = shots.findIndex((s) => s.id === state.selectedShotId);
  const nextIdx = clamp((idx < 0 ? 0 : idx) + delta, 0, shots.length - 1);
  setState({ selectedShotId: shots[nextIdx].id, previewMode: "shot" });
}

function updateShotField(shotId, field, value) {
  const shot = state.draftPlan && state.draftPlan.shots.find((s) => s.id === shotId);
  if (!shot) return;
  shot[field] = value;
  setState({ dirty: true });
}

function updateShotTrim(shotId, start, end) {
  const shot = state.draftPlan && state.draftPlan.shots.find((s) => s.id === shotId);
  if (!shot) return;
  shot.trim = { start: Math.round(start * 10) / 10, end: Math.round(end * 10) / 10 };
  setState({ dirty: true });
}

function toggleShotEnabled(shotId) {
  const shot = state.draftPlan && state.draftPlan.shots.find((s) => s.id === shotId);
  if (!shot) return;
  shot.enabled = !shot.enabled;
  setState({ dirty: true });
}

function updateBgm(field, value) {
  if (!state.draftPlan) return;
  if (!state.draftPlan.bgm) state.draftPlan.bgm = { file: null, gain_db: -14, ducking: true };
  state.draftPlan.bgm[field] = value;
  setState({ dirty: true });
}

function addSfx() {
  if (!state.draftPlan) return;
  const firstAsset = state.assets.sfx[0];
  state.draftPlan.sfx = state.draftPlan.sfx || [];
  state.draftPlan.sfx.push({ file: firstAsset ? firstAsset.file : "", at_sec: 0, gain_db: -8 });
  setState({ dirty: true });
}

function removeSfx(index) {
  if (!state.draftPlan || !state.draftPlan.sfx) return;
  state.draftPlan.sfx.splice(index, 1);
  setState({ dirty: true, highlightedSfxIndex: null });
}

function updateSfx(index, field, value) {
  const item = state.draftPlan && state.draftPlan.sfx && state.draftPlan.sfx[index];
  if (!item) return;
  item[field] = value;
  setState({ dirty: true });
}

function updateSubtitleStyle(field, value) {
  if (!state.draftPlan) return;
  if (!state.draftPlan.subtitle_style) state.draftPlan.subtitle_style = { font_size: 48, accent_color: "#FFD84D", position: "lower", preset: "default" };
  state.draftPlan.subtitle_style[field] = value;
  setState({ dirty: true });
}

async function savePlan() {
  if (!state.current || !state.draftPlan) return;
  setState({ saving: true });
  try {
    await api.putPlan(state.current.id, state.draftPlan);
    setState({ saving: false, dirty: false, current: { ...state.current, plan: deepClone(state.draftPlan) } });
    showToast({ type: "success", title: "保存しました" });
  } catch (err) {
    setState({ saving: false });
    showApiError(err, "保存に失敗しました");
  }
}

async function startRenderAction() {
  if (!state.current) return;
  if (state.dirty) {
    await savePlan();
    if (state.dirty) return; // 保存失敗時は書き出しへ進まない
  }
  try {
    const { job_id } = await api.startRender(state.current.id);
    setState({
      current: { ...state.current, status: "rendering" },
      projects: state.projects.map((p) => (p.id === state.current.id ? { ...p, status: "rendering" } : p)),
    });
    subscribeToJob(job_id, "render");
  } catch (err) {
    showApiError(err, "書き出しを開始できませんでした");
  }
}

// ---------- プレビュー / タイムライン補助操作 ----------
function toggleFakePlayback() { setState({ fakePlaying: !state.fakePlaying }); }
function setPreviewMode(mode) { setState({ previewMode: mode }); }

function focusBgmField() {
  requestAnimationFrame(() => {
    const el = document.getElementById("bgm-file");
    if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); el.focus(); }
  });
}

function highlightSfx(index) {
  setState({ highlightedSfxIndex: index });
  requestAnimationFrame(() => {
    const el = document.querySelector(`[data-sfx-index="${index}"]`);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  });
  setTimeout(() => { if (state.highlightedSfxIndex === index) setState({ highlightedSfxIndex: null }); }, 2500);
}

// ---------- アセットブラウザ ----------
function switchAssetTab(tab) { setState({ assets: { ...state.assets, tab } }); }

function toggleAssetPlayback(kind, file) {
  const already = state.assets.playing && state.assets.playing.kind === kind && state.assets.playing.file === file;
  if (assetAudioTimer) { clearTimeout(assetAudioTimer); assetAudioTimer = null; }
  if (already) {
    if (sharedAudio) sharedAudio.pause();
    setState({ assets: { ...state.assets, playing: null } });
    return;
  }
  setState({ assets: { ...state.assets, playing: { kind, file } } });

  if (MOCK) {
    if (!mockAudioNoticeShown) {
      mockAudioNoticeShown = true;
      showToast({ type: "info", title: "試聴について", message: "モックモードのため実音声はありません。再生インジケータのみのシミュレーションです。" });
    }
    const durationMs = kind === "bgm" ? 4000 : 1200;
    assetAudioTimer = setTimeout(() => { setState({ assets: { ...state.assets, playing: null } }); }, durationMs);
  } else {
    const url = api.mediaUrl(kind, file);
    const audio = getSharedAudio();
    audio.src = url;
    audio.onended = () => setState({ assets: { ...state.assets, playing: null } });
    audio.play().catch(() => {
      setState({ assets: { ...state.assets, playing: null } });
      showApiError(new Error("音声を再生できませんでした"), "試聴エラー");
    });
  }
}

// ---------- ヘッダー ----------
function iconBack() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>`;
}

function renderHeader() {
  const el = document.getElementById("app-header");
  const project = state.current;
  const job = state.job;

  const saveDisabled = !project || !state.draftPlan || !state.dirty || state.saving;
  const renderDisabled = !project || !state.draftPlan || project.status === "generating" || project.status === "rendering";

  el.innerHTML = `
    <div class="app-header__brand"><span class="logo-dot" aria-hidden="true"></span><span>Reel Studio</span></div>
    <div class="app-header__project">
      ${project ? `
        <button class="btn btn--icon btn--ghost" data-action="close-project" aria-label="一覧に戻る">${iconBack()}</button>
        <span class="app-header__project-title">${escapeHtml(project.theme)}</span>
        <span class="badge badge--${project.status}">${STATUS_LABEL[project.status] || project.status}</span>
        ${state.dirty ? `<span class="unsaved-dot" role="status" aria-label="未保存の変更があります" title="未保存の変更があります"></span>` : ""}
      ` : `<span class="field-hint">プロジェクト未選択</span>`}
    </div>
    ${job && !job.done ? `
      <div class="header-progress" role="status" aria-live="polite">
        <span>${job.kind === "render" ? "書き出し" : "生成"} ${job.progress}%</span>
        <div class="progress-bar"><div class="progress-bar__fill" style="width:${job.progress}%"></div></div>
      </div>` : ""}
    <div class="app-header__actions">
      <button class="btn" data-action="save-plan" ${saveDisabled ? "disabled" : ""} aria-disabled="${saveDisabled}" aria-label="プランを保存">
        ${state.saving ? "保存中…" : "保存"}
      </button>
      <button class="btn btn--accent2" data-action="start-render" ${renderDisabled ? "disabled" : ""} aria-disabled="${renderDisabled}" aria-label="動画を書き出し">
        書き出し
      </button>
    </div>
  `;

  const closeBtn = el.querySelector('[data-action="close-project"]');
  if (closeBtn) closeBtn.addEventListener("click", closeProject);
  el.querySelector('[data-action="save-plan"]').addEventListener("click", savePlan);
  el.querySelector('[data-action="start-render"]').addEventListener("click", startRenderAction);
}

// ---------- 描画ディスパッチ ----------
const actions = {
  loadProjects, openProject, reopenCurrent, closeProject,
  toggleCreateForm, submitCreateProject,
  selectShot, moveShotSelection,
  updateShotField, updateShotTrim, toggleShotEnabled,
  updateBgm, addSfx, removeSfx, updateSfx, updateSubtitleStyle,
  savePlan, startRender: startRenderAction,
  toggleFakePlayback, setPreviewMode, focusBgmField, highlightSfx,
  switchAssetTab, toggleAssetPlayback, loadAssets,
};

function render() {
  renderHeader();
  renderProjectList(document.getElementById("pane-projects"), state, actions);
  renderAssetBrowser(document.getElementById("pane-assets"), state, actions);
  renderPreview(document.getElementById("pane-preview"), state, actions);
  renderInspector(document.getElementById("pane-inspector"), state, actions);
  renderTimeline(document.getElementById("pane-timeline"), state, actions);
}

// ---------- キーボードショートカット ----------
document.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName || "").toLowerCase();
  const isTyping = tag === "input" || tag === "textarea" || tag === "select" || (e.target && e.target.isContentEditable);
  if (isTyping) return;

  if (e.code === "Space") {
    if (!state.current) return;
    e.preventDefault();
    const videoEl = document.getElementById("preview-video");
    if (videoEl) { if (videoEl.paused) videoEl.play().catch(() => {}); else videoEl.pause(); }
    else toggleFakePlayback();
  } else if (e.key === "ArrowRight") {
    if (!state.draftPlan) return;
    e.preventDefault();
    moveShotSelection(1);
  } else if (e.key === "ArrowLeft") {
    if (!state.draftPlan) return;
    e.preventDefault();
    moveShotSelection(-1);
  }
});

// ---------- 初期化（?mock=1&open=<id>&selected=<shotId>&autorender=1 でQA時の状態を固定） ----------
async function init() {
  render();
  await Promise.all([loadProjects(), loadAssets()]);

  const openId = qsParams.get("open");
  if (openId) {
    await openProject(openId);
    const selectedParam = qsParams.get("selected");
    if (selectedParam && state.draftPlan && state.draftPlan.shots.some((s) => s.id === selectedParam)) {
      setState({ selectedShotId: selectedParam });
    }
    if (qsParams.get("autorender") === "1" && state.current && state.current.status === "ready") {
      await startRenderAction();
    }
  }
}

init();
