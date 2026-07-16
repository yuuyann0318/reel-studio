// 左ペイン・下段: アセットブラウザ（BGM / SFX 試聴）
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const MOOD_LABEL = { upbeat: "アップビート", calm: "穏やか", emotional: "エモーショナル" };
const CATEGORY_LABEL = { impact: "インパクト", ui: "UI", transition: "トランジション" };

export function renderAssetBrowser(container, state, actions) {
  const { assets } = state;
  const tab = assets.tab || "bgm";
  const items = tab === "bgm" ? assets.bgm : assets.sfx;

  let bodyHtml;
  if (assets.loading) {
    bodyHtml = `<div class="skeleton skeleton-row" style="height:40px;"></div><div class="skeleton skeleton-row" style="height:40px;"></div><div class="skeleton skeleton-row" style="height:40px;"></div>`;
  } else if (assets.error) {
    bodyHtml = `
      <div class="state-block">
        <div class="state-block__icon">${iconAlert()}</div>
        <div class="state-block__title">素材を読み込めませんでした</div>
        <div class="state-block__desc">${escapeHtml(assets.error.message)}${assets.error.code ? ` (code: ${escapeHtml(assets.error.code)})` : ""}</div>
        <button class="btn btn--sm" data-action="retry-assets">再読み込み</button>
      </div>`;
  } else if (!items || items.length === 0) {
    bodyHtml = `
      <div class="state-block">
        <div class="state-block__icon">${iconMusic()}</div>
        <div class="state-block__title">${tab === "bgm" ? "BGM" : "効果音"}がありません</div>
      </div>`;
  } else {
    bodyHtml = items
      .map((item) => {
        const isPlaying = assets.playing && assets.playing.kind === tab && assets.playing.file === item.file;
        const metaLabel = tab === "bgm" ? MOOD_LABEL[item.mood] || item.mood : CATEGORY_LABEL[item.category] || item.category;
        return `
        <div class="asset-row ${isPlaying ? "is-playing" : ""}">
          <button class="play-btn" data-action="toggle-play" data-kind="${tab}" data-file="${escapeHtml(item.file)}" aria-label="${isPlaying ? "停止" : "試聴"}: ${escapeHtml(item.file)}">
            ${isPlaying ? iconPause() : iconPlay()}
          </button>
          <span class="asset-row__name">${escapeHtml(item.file)}</span>
          <span class="asset-row__meta">${escapeHtml(metaLabel || "")}</span>
        </div>`;
      })
      .join("");
  }

  container.innerHTML = `
    <div class="section-heading">
      <span>アセット</span>
    </div>
    <div class="asset-tabs" role="tablist" aria-label="アセット種別">
      <button class="asset-tab" role="tab" aria-selected="${tab === "bgm"}" data-action="tab" data-tab="bgm">BGM</button>
      <button class="asset-tab" role="tab" aria-selected="${tab === "sfx"}" data-action="tab" data-tab="sfx">効果音</button>
    </div>
    <div class="scroll-y" style="flex:1;">${bodyHtml}</div>
  `;

  container.querySelectorAll('[data-action="tab"]').forEach((el) => {
    el.addEventListener("click", () => actions.switchAssetTab(el.dataset.tab));
  });
  container.querySelectorAll('[data-action="toggle-play"]').forEach((el) => {
    el.addEventListener("click", () => actions.toggleAssetPlayback(el.dataset.kind, el.dataset.file));
  });
  const retryBtn = container.querySelector('[data-action="retry-assets"]');
  if (retryBtn) retryBtn.addEventListener("click", () => actions.loadAssets());
}

function iconAlert() {
  return `<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>`;
}
function iconMusic() {
  return `<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`;
}
function iconPlay() {
  return `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7Z"/></svg>`;
}
function iconPause() {
  return `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>`;
}
