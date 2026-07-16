// 中央ペイン: 9:16 プレビュー（video タグ / Range配信前提）
import { api, MOCK } from "../api.js";

const STAGE_ICON = `<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 10h16v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9Z"/><path d="m4 10 1.5-5 3 1-1.2 4M9.5 10l1-5 3 .8-1 4.2M15 10l1.2-4.6 3 .9-1.2 3.7"/></svg>`;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function renderPreview(container, state, actions) {
  const project = state.current;

  if (state.currentLoading) {
    container.innerHTML = `
      <div class="state-block">
        <div class="spinner" aria-hidden="true"></div>
        <div class="state-block__title">プロジェクトを読み込んでいます…</div>
      </div>`;
    return;
  }
  if (state.currentError) {
    container.innerHTML = `
      <div class="state-block">
        <div class="state-block__icon">${iconAlert()}</div>
        <div class="state-block__title">読み込みに失敗しました</div>
        <div class="state-block__desc">${escapeHtml(state.currentError.message)}${state.currentError.code ? ` (code: ${escapeHtml(state.currentError.code)})` : ""}</div>
        <button class="btn btn--sm" data-action="retry">再読み込み</button>
      </div>`;
    container.querySelector('[data-action="retry"]').addEventListener("click", () => actions.reopenCurrent());
    return;
  }
  if (!project) {
    container.innerHTML = `
      <div class="preview-empty">
        ${STAGE_ICON}
        <div class="state-block__title">プロジェクト未選択</div>
        <div class="state-block__desc">左のリストからプロジェクトを選ぶか、「＋ 新規作成」でテーマから生成しましょう</div>
      </div>`;
    return;
  }

  const isGenerating = project.status === "generating";
  const isRendering = project.status === "rendering";
  const job = state.job && state.job.projectId === project.id ? state.job : null;

  const plan = project.plan;
  const shots = (plan && plan.shots) || [];
  const selectedShot = shots.find((s) => s.id === state.selectedShotId) || shots[0] || null;
  const previewMode = state.previewMode === "final" && project.renders && project.renders.length ? "final" : "shot";
  const subtitleStyle = (plan && plan.subtitle_style) || { font_size: 48, accent_color: "#FFD84D", position: "lower" };

  let mediaSrc = "";
  if (previewMode === "final" && project.renders && project.renders.length) {
    mediaSrc = api.mediaUrl("raw", project.renders[project.renders.length - 1].path);
  } else if (selectedShot) {
    mediaSrc = api.mediaUrl("raw", selectedShot.clip_path);
  }

  const overlayHtml = isGenerating || isRendering
    ? `
    <div class="preview-overlay">
      <div class="spinner" aria-hidden="true"></div>
      <div class="state-block__title">${isRendering ? "書き出し中…" : "生成中…"}</div>
      <div class="progress-bar"><div class="progress-bar__fill" style="width:${job ? job.progress : 4}%"></div></div>
      <div class="progress-label">${escapeHtml((job && job.message) || "しばらくお待ちください")}</div>
    </div>`
    : "";

  const scale = Math.min(1, 340 / 1080); // プレビュー枠の目安幅からフォントサイズを縮小換算
  const fontPx = Math.max(14, Math.round((subtitleStyle.font_size || 48) * 0.32));

  const showOverlay = isGenerating || isRendering;
  let frameInnerHtml;
  if (!mediaSrc) {
    frameInnerHtml = `
      <div class="preview-frame__placeholder">
        ${showOverlay ? "" : MOCK ? "モックプレビュー（実映像なし）" : "映像を読み込めません"}
      </div>
      ${!showOverlay && previewMode === "shot" && selectedShot ? `<div class="preview-caption preview-caption--${subtitleStyle.position || "lower"}" style="font-size:${fontPx}px; color:${escapeHtml(subtitleStyle.accent_color || "#FFD84D")};">${escapeHtml(selectedShot.caption || "")}</div>` : ""}
      ${showOverlay ? "" : `
      <div class="preview-controls">
        <button class="btn btn--icon" data-action="toggle-fake-play" aria-label="${state.fakePlaying ? "停止" : "再生"}">${state.fakePlaying ? iconPause() : iconPlay()}</button>
        <div class="preview-seek"><div class="progress-bar"><div class="progress-bar__fill" style="width:${state.fakePlaying ? 60 : 0}%"></div></div></div>
        <span class="preview-time">${previewMode === "shot" && selectedShot ? `Shot ${shots.indexOf(selectedShot) + 1}/${shots.length}` : "--:--"}</span>
      </div>`}
    `;
  } else {
    frameInnerHtml = `
      <video id="preview-video" src="${escapeHtml(mediaSrc)}" controls preload="metadata" aria-label="プレビュー映像"></video>
      ${!showOverlay && previewMode === "shot" && selectedShot ? `<div class="preview-caption preview-caption--${subtitleStyle.position || "lower"}" style="font-size:${fontPx}px; color:${escapeHtml(subtitleStyle.accent_color || "#FFD84D")};">${escapeHtml(selectedShot.caption || "")}</div>` : ""}
    `;
  }

  container.innerHTML = `
    <div class="preview-frame">
      ${frameInnerHtml}
      ${overlayHtml}
    </div>
    <div style="margin-top:var(--space-3); display:flex; gap:8px; align-items:center;">
      ${previewMode === "final"
        ? `<button class="btn btn--sm" data-action="back-to-shot">編集プレビューに戻る</button><span class="tag-mono">完成プレビュー</span>`
        : shots.length
          ? `<span class="tag-mono">${selectedShot ? `${escapeHtml(selectedShot.caption || "(無題)")}` : "ショット未選択"}</span>`
          : ""}
      ${project.renders && project.renders.length && previewMode !== "final" ? `<button class="btn btn--sm" data-action="show-final">完成プレビューを見る</button>` : ""}
    </div>
  `;

  const fakePlayBtn = container.querySelector('[data-action="toggle-fake-play"]');
  if (fakePlayBtn) fakePlayBtn.addEventListener("click", () => actions.toggleFakePlayback());
  const backBtn = container.querySelector('[data-action="back-to-shot"]');
  if (backBtn) backBtn.addEventListener("click", () => actions.setPreviewMode("shot"));
  const finalBtn = container.querySelector('[data-action="show-final"]');
  if (finalBtn) finalBtn.addEventListener("click", () => actions.setPreviewMode("final"));
}

function iconAlert() {
  return `<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>`;
}
function iconPlay() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7Z"/></svg>`;
}
function iconPause() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>`;
}
