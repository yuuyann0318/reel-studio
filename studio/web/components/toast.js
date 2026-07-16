// トースト通知（エラーは error.code を必ず併記）
let container = null;
function ensureContainer() {
  if (container) return container;
  container = document.createElement("div");
  container.className = "toast-stack";
  container.setAttribute("role", "region");
  container.setAttribute("aria-label", "通知");
  container.setAttribute("aria-live", "polite");
  document.body.appendChild(container);
  return container;
}

const ICONS = {
  success: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>`,
  error: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>`,
  info: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>`,
};

export function showToast({ type = "info", title, message, code, duration = 5200 }) {
  const el = ensureContainer();
  const toast = document.createElement("div");
  toast.className = `toast toast--${type}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.innerHTML = `
    <div class="toast__icon" style="color:${type === "error" ? "var(--danger)" : type === "success" ? "var(--success)" : "var(--accent)"}">${ICONS[type] || ICONS.info}</div>
    <div class="toast__body">
      <div class="toast__title">${escapeHtml(title || (type === "error" ? "エラーが発生しました" : "通知"))}</div>
      ${message ? `<div class="toast__desc">${escapeHtml(message)}</div>` : ""}
      ${code ? `<div class="toast__code">code: ${escapeHtml(code)}</div>` : ""}
    </div>
    <button class="toast__close btn btn--icon btn--ghost" aria-label="通知を閉じる">✕</button>
  `;
  const close = () => { toast.remove(); };
  toast.querySelector(".toast__close").addEventListener("click", close);
  el.appendChild(toast);
  if (duration > 0) setTimeout(close, duration);
  return close;
}

export function showApiError(err, fallbackTitle = "処理に失敗しました") {
  showToast({
    type: "error",
    title: fallbackTitle,
    message: err && err.message,
    code: err && err.code,
    duration: 8000,
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
