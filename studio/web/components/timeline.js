// 下段: タイムライン（ショット / BGM帯 / SFXマーカー / トリムハンドル）
const PX_PER_SEC = 42;
const MIN_BLOCK_PX = 34;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }

export function renderTimeline(container, state, actions) {
  const project = state.current;
  const plan = state.draftPlan;

  if (state.currentLoading) {
    container.innerHTML = `<div class="state-block" style="padding:var(--space-3);"><div class="spinner" aria-hidden="true"></div></div>`;
    return;
  }
  if (!project || !plan) {
    container.innerHTML = `
      <div class="state-block" style="padding:var(--space-3);">
        <div class="state-block__desc">プロジェクトを開くとタイムラインが表示されます</div>
      </div>`;
    return;
  }

  const shots = (plan.shots || []).slice().sort((a, b) => a.order - b.order);
  const durations = shots.map((s) => Math.max(0.1, s.trim.end - s.trim.start));
  const totalDuration = Math.max(5, durations.reduce((a, b) => a + b, 0));
  const widthPx = Math.max(600, totalDuration * PX_PER_SEC + 80);

  // ルーラー目盛り（5秒間隔）
  let ruler = "";
  for (let t = 0; t <= totalDuration + 5; t += 5) {
    ruler += `<div class="timeline-ruler__tick" style="left:${t * PX_PER_SEC}px;">${t}s</div>`;
  }

  // BGMトラック
  const bgm = plan.bgm;
  const bgmHtml = bgm && bgm.file
    ? `<div class="bgm-band" style="left:0px; width:${totalDuration * PX_PER_SEC}px;" title="${escapeHtml(bgm.file)}" data-action="focus-bgm" role="button" tabindex="0">🎵 ${escapeHtml(bgm.file)}（${bgm.gain_db}dB）</div>`
    : `<div class="bgm-band" style="left:0px; width:${totalDuration * PX_PER_SEC}px; opacity:0.35; border-style:dashed;" data-action="focus-bgm" role="button" tabindex="0">BGM未設定（インスペクタで選択）</div>`;

  // SFXマーカー
  const sfxHtml = (plan.sfx || [])
    .map((sfx, i) => {
      const highlighted = state.highlightedSfxIndex === i ? "is-selected" : "";
      return `<div class="sfx-marker ${highlighted}" style="left:${sfx.at_sec * PX_PER_SEC}px;" data-action="pick-sfx" data-index="${i}" title="${escapeHtml(sfx.file)} @ ${sfx.at_sec}s" tabindex="0" role="button" aria-label="効果音 ${escapeHtml(sfx.file)} ${sfx.at_sec}秒"></div>`;
    })
    .join("");

  // ショットブロック
  let cursor = 0;
  const shotBlocks = shots
    .map((shot, i) => {
      const dur = durations[i];
      const left = cursor * PX_PER_SEC;
      const width = Math.max(MIN_BLOCK_PX, dur * PX_PER_SEC);
      cursor += dur;
      const selected = state.selectedShotId === shot.id;
      return `
      <div class="shot-block ${shot.enabled ? "" : "is-disabled"}" style="left:${left}px; width:${width}px;"
           data-shot-id="${escapeHtml(shot.id)}" aria-selected="${selected}" tabindex="0"
           aria-label="ショット${i + 1}: ${escapeHtml(shot.caption)}${shot.enabled ? "" : "（無効）"}">
        <div class="trim-handle trim-handle--left" data-shot-id="${escapeHtml(shot.id)}" data-edge="start"></div>
        <div class="shot-block__thumb">
          ${!shot.enabled ? `<span class="shot-block__badge">無効</span>` : ""}
        </div>
        <div class="shot-block__label">${i + 1}. ${escapeHtml(shot.caption)}</div>
        <div class="trim-handle trim-handle--right" data-shot-id="${escapeHtml(shot.id)}" data-edge="end"></div>
      </div>`;
    })
    .join("");

  container.innerHTML = `
    <div class="timeline-toolbar">
      <span class="tag-mono">合計 ${totalDuration.toFixed(1)}s（有効ショットのみレンダリング対象）</span>
      <span class="field-hint">ドラッグでトリム・クリックで選択・← → で移動</span>
    </div>
    <div class="timeline-body">
      <div class="timeline-tracklabels">
        <div style="height:28px;">時間</div>
        <div class="timeline-track--bgm" style="height:44px;">BGM</div>
        <div style="height:72px;">ショット</div>
      </div>
      <div class="timeline-scroll scroll-x">
        <div class="timeline-tracks" style="width:${widthPx}px;">
          <div class="timeline-ruler">${ruler}</div>
          <div class="timeline-track timeline-track--bgm">${bgmHtml}${sfxHtml}</div>
          <div class="timeline-track timeline-track--shots">${shotBlocks || `<div class="field-hint" style="padding:8px 12px;">ショットがありません</div>`}</div>
        </div>
      </div>
    </div>
  `;

  // --- イベント: ショット選択 & トリムドラッグ ---
  container.querySelectorAll(".shot-block").forEach((block) => {
    const shotId = block.dataset.shotId;
    block.addEventListener("click", (e) => {
      if (e.target.classList.contains("trim-handle")) return;
      actions.selectShot(shotId);
    });
    block.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); actions.selectShot(shotId); }
    });
  });

  container.querySelectorAll(".trim-handle").forEach((handle) => {
    handle.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      e.preventDefault();
      const shotId = handle.dataset.shotId;
      const edge = handle.dataset.edge;
      const shot = shots.find((s) => s.id === shotId);
      if (!shot) return;
      const startX = e.clientX;
      const origStart = shot.trim.start;
      const origEnd = shot.trim.end;
      const sourceDur = shot.source_duration || (origEnd - origStart);

      function onMove(ev) {
        const deltaSec = (ev.clientX - startX) / PX_PER_SEC;
        if (edge === "start") {
          const ns = clamp(origStart + deltaSec, 0, origEnd - 0.1);
          actions.updateShotTrim(shotId, ns, origEnd);
        } else {
          const ne = clamp(origEnd + deltaSec, origStart + 0.1, sourceDur);
          actions.updateShotTrim(shotId, origStart, ne);
        }
      }
      function onUp() {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
      }
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    });
  });

  const bgmBand = container.querySelector('[data-action="focus-bgm"]');
  if (bgmBand) bgmBand.addEventListener("click", () => actions.focusBgmField());

  container.querySelectorAll('[data-action="pick-sfx"]').forEach((el) => {
    el.addEventListener("click", () => actions.highlightSfx(Number(el.dataset.index)));
  });
}
