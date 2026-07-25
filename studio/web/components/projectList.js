// 左ペイン・上段: プロジェクト一覧 + 新規作成フォーム
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

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function renderProjectList(container, state, actions) {
  const { projectsLoading, projectsError, projects, current, showCreateForm, createForm, createSubmitting, createError } = state;

  let listHtml;
  if (projectsLoading) {
    listHtml = `
      <div class="skeleton skeleton-row"></div>
      <div class="skeleton skeleton-row"></div>
      <div class="skeleton skeleton-row"></div>
    `;
  } else if (projectsError) {
    listHtml = `
      <div class="state-block">
        <div class="state-block__icon">${iconAlert()}</div>
        <div class="state-block__title">一覧を読み込めませんでした</div>
        <div class="state-block__desc">${escapeHtml(projectsError.message)}${projectsError.code ? ` (code: ${escapeHtml(projectsError.code)})` : ""}</div>
        <button class="btn btn--sm" data-action="retry-projects">再読み込み</button>
      </div>
    `;
  } else if (!projects || projects.length === 0) {
    listHtml = `
      <div class="state-block">
        <div class="state-block__icon">${iconFolder()}</div>
        <div class="state-block__title">プロジェクトがありません</div>
        <div class="state-block__desc">「新規作成」からテーマを入力して最初のリールを作りましょう</div>
      </div>
    `;
  } else {
    listHtml = projects
      .map((p) => {
        const isCurrent = current && current.id === p.id;
        return `
        <div class="project-card" role="button" tabindex="0" data-action="open-project" data-id="${escapeHtml(p.id)}"
             aria-current="${isCurrent ? "true" : "false"}" aria-label="プロジェクト: ${escapeHtml(p.theme)} (${STATUS_LABEL[p.status] || p.status})">
          <div class="project-card__thumb" aria-hidden="true">${iconClap()}</div>
          <div class="project-card__body">
            <div class="project-card__title">${escapeHtml(p.theme)}</div>
            <div class="project-card__meta">
              <span class="badge badge--${p.status}">${STATUS_LABEL[p.status] || p.status}</span>
              <span>${fmtDate(p.created_at)}</span>
            </div>
          </div>
        </div>`;
      })
      .join("");
  }

  const formHtml = showCreateForm
    ? `
    <form class="inline-form" id="create-project-form" novalidate>
      <div class="field">
        <label for="f-theme">テーマ</label>
        <input class="input" id="f-theme" name="theme" type="text" placeholder="例: 朝活で人生を変える3つの習慣" value="${escapeHtml(createForm.theme)}" required maxlength="80" autocomplete="off" />
      </div>
      <div class="field">
        <label for="f-duration">尺（秒）</label>
        <select class="select" id="f-duration" name="duration">
          ${[15, 30, 45, 60].map((d) => `<option value="${d}" ${createForm.duration === d ? "selected" : ""}>${d}秒</option>`).join("")}
        </select>
      </div>
      <div class="field">
        <label for="f-plan">プラン（ワンセット）</label>
        <select class="select" id="f-plan" name="plan_tier">
          <option value="free" ${(createForm.planTier || "free") === "free" ? "selected" : ""}>むりょうコース（映像mock・声say・0円）</option>
          <option value="paid" ${createForm.planTier === "paid" ? "selected" : ""}>本番コース（映像higgsfield・声Fish Audio・コイン消費）</option>
        </select>
        <div class="field-hint">迷ったら「むりょうコース」（外部の有料APIを一切使わず0円で動作確認できます）</div>
      </div>
      <div class="field">
        <label for="f-style">テロップ・カットのスタイル</label>
        <select class="select" id="f-style" name="style">
          <option value="default" ${(createForm.style || "default") === "default" ? "selected" : ""}>標準</option>
          <option value="vertical_hook" ${createForm.style === "vertical_hook" ? "selected" : ""}>縦書きフック（高速カット）</option>
        </select>
        <div class="field-hint">縦書きフックは明朝体の縦組みテロップ＋約2秒刻みの高速カット構成</div>
      </div>
      <div class="field">
        <label for="f-reference-url">参考動画URL（必須）</label>
        <input class="input" id="f-reference-url" name="reference_url" type="url" placeholder="https://www.tiktok.com/@example/video/123" value="${escapeHtml(createForm.referenceUrl || "")}" required maxlength="500" autocomplete="off" />
        <div class="field-hint">TTP v2 では参考動画URLが必須です。構成・テンポを再現しますが文言はコピーしません</div>
      </div>
      ${createError ? `<div class="field-hint" style="color:var(--danger)" role="alert">${escapeHtml(createError.message)}${createError.code ? ` (code: ${escapeHtml(createError.code)})` : ""}</div>` : ""}
      <div class="form-actions">
        <button type="submit" class="btn btn--primary btn--block" ${createSubmitting ? "disabled" : ""}>${createSubmitting ? "作成中…" : "生成開始"}</button>
        <button type="button" class="btn" data-action="cancel-create">キャンセル</button>
      </div>
    </form>`
    : "";

  container.innerHTML = `
    <div class="section-heading">
      <span>プロジェクト</span>
      <button class="btn btn--sm btn--primary" data-action="toggle-create" aria-expanded="${showCreateForm ? "true" : "false"}" aria-controls="create-project-form">
        ${showCreateForm ? "閉じる" : "＋ 新規作成"}
      </button>
    </div>
    <div class="scroll-y" style="flex:1; padding-top:4px;">${listHtml}</div>
    ${formHtml}
  `;

  container.querySelectorAll('[data-action="open-project"]').forEach((el) => {
    el.addEventListener("click", () => actions.openProject(el.dataset.id));
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); actions.openProject(el.dataset.id); }
    });
  });
  const retryBtn = container.querySelector('[data-action="retry-projects"]');
  if (retryBtn) retryBtn.addEventListener("click", () => actions.loadProjects());

  const toggleBtn = container.querySelector('[data-action="toggle-create"]');
  if (toggleBtn) toggleBtn.addEventListener("click", () => actions.toggleCreateForm());
  const cancelBtn = container.querySelector('[data-action="cancel-create"]');
  if (cancelBtn) cancelBtn.addEventListener("click", () => actions.toggleCreateForm(false));

  const form = container.querySelector("#create-project-form");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const referenceUrl = String(fd.get("reference_url") || "").trim();
      if (!referenceUrl) {
        // TTP v2 移行後、参考動画URLは必須。空なら送信せずエラーメッセージを出す。
        actions.setCreateError({ message: "参考動画URLが必要です（TTP v2）", code: "reference_url_required" });
        return;
      }
      actions.submitCreateProject({
        theme: fd.get("theme"),
        duration: Number(fd.get("duration")),
        planTier: fd.get("plan_tier"),
        style: fd.get("style"),
        referenceUrl,
      });
    });
  }
}

function iconAlert() {
  return `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>`;
}
function iconFolder() {
  return `<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>`;
}
function iconClap() {
  return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 10h16v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9Z"/><path d="m4 10 1.5-5 3 1-1.2 4M9.5 10l1-5 3 .8-1 4.2M15 10l1.2-4.6 3 .9-1.2 3.7"/></svg>`;
}
