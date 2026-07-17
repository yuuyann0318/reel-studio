// 右ペイン: インスペクタ（ショット編集 / BGM / SFX / 字幕スタイル）
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtSec(n) { return `${Number(n).toFixed(1)}s`; }

export function renderInspector(container, state, actions) {
  const project = state.current;
  const plan = state.draftPlan;

  if (state.currentLoading) {
    container.innerHTML = `<div class="state-block"><div class="spinner" aria-hidden="true"></div><div class="state-block__title">読み込み中…</div></div>`;
    return;
  }
  if (!project || !plan) {
    container.innerHTML = `
      <div class="state-block">
        ${iconInspector()}
        <div class="state-block__title">インスペクタ</div>
        <div class="state-block__desc">プロジェクトを開くと、ショット・BGM・効果音・字幕スタイルを編集できます</div>
      </div>`;
    return;
  }

  const shots = plan.shots || [];
  const selectedShot = shots.find((s) => s.id === state.selectedShotId) || null;
  const bgm = plan.bgm || { file: "", gain_db: -14, ducking: true };
  const sfxList = plan.sfx || [];
  const style = plan.subtitle_style || { font_size: 48, accent_color: "#FFD84D", position: "lower", preset: "default" };
  const bgmAssets = state.assets.bgm || [];
  const sfxAssets = state.assets.sfx || [];

  container.innerHTML = `
    <div class="inspector-section">
      <h3>選択中のショット</h3>
      ${selectedShot ? shotEditorHtml(selectedShot) : `<div class="field-hint">タイムラインでショットを選択すると編集できます</div>`}
    </div>

    <div class="inspector-section">
      <h3>BGM</h3>
      <div class="field">
        <label for="bgm-file">曲</label>
        <select class="select" id="bgm-file" ${bgmAssets.length === 0 ? "disabled" : ""}>
          <option value="" ${!bgm.file ? "selected" : ""}>なし</option>
          ${bgmAssets.map((a) => `<option value="${escapeHtml(a.file)}" ${bgm.file === a.file ? "selected" : ""}>${escapeHtml(a.file)}（${escapeHtml(a.mood || "")}）</option>`).join("")}
        </select>
      </div>
      <div class="field">
        <label for="bgm-gain">音量 (gain dB)</label>
        <div class="slider-row">
          <input type="range" id="bgm-gain" min="-30" max="0" step="1" value="${bgm.gain_db}" aria-valuemin="-30" aria-valuemax="0" aria-valuenow="${bgm.gain_db}" />
          <span class="slider-value">${bgm.gain_db} dB</span>
        </div>
      </div>
      <div class="field" style="flex-direction:row; align-items:center; justify-content:space-between;">
        <label for="bgm-ducking" style="margin:0;">ナレーション時に自動ダッキング</label>
        <button class="toggle" id="bgm-ducking" role="switch" aria-checked="${!!bgm.ducking}" aria-label="ダッキング"></button>
      </div>
    </div>

    <div class="inspector-section">
      <h3>効果音（SFX）<button class="btn btn--sm btn--ghost" data-action="add-sfx" aria-label="効果音を追加">＋ 追加</button></h3>
      ${sfxList.length === 0 ? `<div class="field-hint">効果音はまだありません</div>` : sfxList.map((sfx, i) => sfxItemHtml(sfx, i, sfxAssets)).join("")}
    </div>

    <div class="inspector-section">
      <h3>字幕スタイル</h3>
      <div class="field">
        <label for="style-preset">スタイル</label>
        <select class="select" id="style-preset">
          <option value="default" ${(style.preset || "default") === "default" ? "selected" : ""}>標準（横書き）</option>
          <option value="vertical_hook" ${style.preset === "vertical_hook" ? "selected" : ""}>縦書きフック（高速カット）</option>
        </select>
        <div class="field-hint">縦書きフックは明朝体・白文字＋黒縁の縦組みテロップになります</div>
      </div>
      <div class="field">
        <label for="style-font-size">フォントサイズ</label>
        <div class="slider-row">
          <input type="range" id="style-font-size" min="32" max="120" step="2" value="${style.font_size}" />
          <span class="slider-value">${style.font_size}px</span>
        </div>
      </div>
      <div class="field" style="flex-direction:row; align-items:center; gap:var(--space-3);">
        <label for="style-color" style="margin:0;">強調色</label>
        <input type="color" id="style-color" value="${escapeHtml(normalizeHex(style.accent_color))}" aria-label="字幕の強調色" />
        <span class="tag-mono">${escapeHtml(style.accent_color)}</span>
      </div>
      <div class="field">
        <label for="style-position">表示位置</label>
        <select class="select" id="style-position">
          <option value="lower" ${style.position === "lower" ? "selected" : ""}>下部</option>
          <option value="center" ${style.position === "center" ? "selected" : ""}>中央</option>
          <option value="upper" ${style.position === "upper" ? "selected" : ""}>上部</option>
        </select>
      </div>
    </div>
  `;

  // --- ショット編集 ---
  if (selectedShot) {
    const captionEl = container.querySelector("#shot-caption");
    if (captionEl) captionEl.addEventListener("change", (e) => actions.updateShotField(selectedShot.id, "caption", e.target.value));

    const enabledToggle = container.querySelector("#shot-enabled");
    if (enabledToggle) enabledToggle.addEventListener("click", () => actions.toggleShotEnabled(selectedShot.id));

    const trimStart = container.querySelector("#trim-start");
    const trimEnd = container.querySelector("#trim-end");
    const trimFill = container.querySelector("#trim-fill");
    const dur = selectedShot.source_duration || 1;
    function updateFill() {
      const s = Number(trimStart.value), e = Number(trimEnd.value);
      trimFill.style.left = `${(s / dur) * 100}%`;
      trimFill.style.width = `${Math.max(0, ((e - s) / dur) * 100)}%`;
      container.querySelector("#trim-label").textContent = `${fmtSec(s)} 〜 ${fmtSec(e)}`;
    }
    if (trimStart && trimEnd) {
      trimStart.addEventListener("input", () => {
        if (Number(trimStart.value) > Number(trimEnd.value) - 0.1) trimStart.value = Math.max(0, Number(trimEnd.value) - 0.1);
        updateFill();
      });
      trimEnd.addEventListener("input", () => {
        if (Number(trimEnd.value) < Number(trimStart.value) + 0.1) trimEnd.value = Math.min(dur, Number(trimStart.value) + 0.1);
        updateFill();
      });
      const commit = () => actions.updateShotTrim(selectedShot.id, Number(trimStart.value), Number(trimEnd.value));
      trimStart.addEventListener("change", commit);
      trimEnd.addEventListener("change", commit);
    }
  }

  // --- BGM ---
  const bgmFile = container.querySelector("#bgm-file");
  if (bgmFile) bgmFile.addEventListener("change", (e) => actions.updateBgm("file", e.target.value || null));
  const bgmGain = container.querySelector("#bgm-gain");
  if (bgmGain) {
    const label = bgmGain.parentElement.querySelector(".slider-value");
    bgmGain.addEventListener("input", () => { label.textContent = `${bgmGain.value} dB`; });
    bgmGain.addEventListener("change", () => actions.updateBgm("gain_db", Number(bgmGain.value)));
  }
  const bgmDucking = container.querySelector("#bgm-ducking");
  if (bgmDucking) bgmDucking.addEventListener("click", () => actions.updateBgm("ducking", !bgm.ducking));

  // --- SFX ---
  const addSfxBtn = container.querySelector('[data-action="add-sfx"]');
  if (addSfxBtn) addSfxBtn.addEventListener("click", () => actions.addSfx());
  container.querySelectorAll("[data-sfx-index]").forEach((row) => {
    const idx = Number(row.dataset.sfxIndex);
    const fileSel = row.querySelector(".sfx-file");
    const atSec = row.querySelector(".sfx-at");
    const gain = row.querySelector(".sfx-gain");
    const removeBtn = row.querySelector(".sfx-remove");
    if (fileSel) fileSel.addEventListener("change", (e) => actions.updateSfx(idx, "file", e.target.value));
    if (atSec) atSec.addEventListener("change", (e) => actions.updateSfx(idx, "at_sec", Number(e.target.value)));
    if (gain) gain.addEventListener("change", (e) => actions.updateSfx(idx, "gain_db", Number(e.target.value)));
    if (removeBtn) removeBtn.addEventListener("click", () => actions.removeSfx(idx));
  });

  // --- 字幕スタイル ---
  const presetSelect = container.querySelector("#style-preset");
  if (presetSelect) presetSelect.addEventListener("change", (e) => actions.updateSubtitleStyle("preset", e.target.value));
  const fontSize = container.querySelector("#style-font-size");
  if (fontSize) {
    const label = fontSize.parentElement.querySelector(".slider-value");
    fontSize.addEventListener("input", () => { label.textContent = `${fontSize.value}px`; });
    fontSize.addEventListener("change", () => actions.updateSubtitleStyle("font_size", Number(fontSize.value)));
  }
  const colorInput = container.querySelector("#style-color");
  if (colorInput) colorInput.addEventListener("change", (e) => actions.updateSubtitleStyle("accent_color", e.target.value));
  const posSelect = container.querySelector("#style-position");
  if (posSelect) posSelect.addEventListener("change", (e) => actions.updateSubtitleStyle("position", e.target.value));
}

function shotEditorHtml(shot) {
  const dur = shot.source_duration || 1;
  return `
    <div class="field">
      <label for="shot-caption">キャプション（テロップ）</label>
      <textarea class="textarea" id="shot-caption">${escapeHtml(shot.caption)}</textarea>
    </div>
    <div class="field">
      <label id="trim-label-heading">トリム範囲 <span id="trim-label" class="tag-mono">${fmtSec(shot.trim.start)} 〜 ${fmtSec(shot.trim.end)}</span></label>
      <div class="dual-range" aria-labelledby="trim-label-heading">
        <div class="dual-range__track"></div>
        <div class="dual-range__fill" id="trim-fill" style="left:${(shot.trim.start / dur) * 100}%; width:${((shot.trim.end - shot.trim.start) / dur) * 100}%;"></div>
        <input type="range" id="trim-start" min="0" max="${dur}" step="0.1" value="${shot.trim.start}" aria-label="トリム開始位置" />
        <input type="range" id="trim-end" min="0" max="${dur}" step="0.1" value="${shot.trim.end}" aria-label="トリム終了位置" />
      </div>
      <div class="field-hint">素材長: ${fmtSec(dur)}</div>
    </div>
    <div class="field" style="flex-direction:row; align-items:center; justify-content:space-between;">
      <label for="shot-enabled" style="margin:0;">このショットを使用する</label>
      <button class="toggle" id="shot-enabled" role="switch" aria-checked="${!!shot.enabled}" aria-label="ショットの有効/無効"></button>
    </div>
  `;
}

function sfxItemHtml(sfx, i, sfxAssets) {
  return `
    <div class="sfx-item" data-sfx-index="${i}">
      <div class="sfx-item__row">
        <select class="select sfx-file" aria-label="効果音ファイル ${i + 1}">
          ${sfxAssets.map((a) => `<option value="${escapeHtml(a.file)}" ${sfx.file === a.file ? "selected" : ""}>${escapeHtml(a.file)}</option>`).join("")}
        </select>
        <button class="btn btn--icon btn--danger sfx-remove" aria-label="効果音 ${i + 1} を削除">✕</button>
      </div>
      <div class="sfx-item__row">
        <label class="field-hint" style="width:52px;">再生位置</label>
        <input type="number" class="input sfx-item__num sfx-at" min="0" step="0.1" value="${sfx.at_sec}" aria-label="再生位置(秒) ${i + 1}" />
        <label class="field-hint" style="width:36px;">gain</label>
        <input type="number" class="input sfx-item__num sfx-gain" step="1" value="${sfx.gain_db}" aria-label="音量(dB) ${i + 1}" />
      </div>
    </div>
  `;
}

function normalizeHex(v) {
  if (!v) return "#FFD84D";
  return /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#FFD84D";
}

function iconInspector() {
  return `<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/></svg>`;
}
