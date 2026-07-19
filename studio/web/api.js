// Reel Studio — API 薄ラッパ
// 契約: docs/STUDIO-DESIGN.md の「API 契約」に厳密準拠。
// ?mock=1 のときはフロント内蔵モック（mock.js）へ全呼び出しを差し替える。

import * as mock from "./mock.js";

const qs = new URLSearchParams(location.search);
export const MOCK = qs.get("mock") === "1";
export const QA = qs; // 画面状態を固定するためのQAクエリ（open/selected/autorender等）をmock.js側で参照

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code);
    this.code = code || "UNKNOWN_ERROR";
    this.status = status || 0;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    throw new ApiError("NETWORK_ERROR", "サーバーに接続できません。バックエンドが起動しているか確認してください。", 0);
  }
  let payload = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try { payload = await res.json(); } catch (e) { payload = null; }
  }
  if (!res.ok) {
    const err = payload && payload.error ? payload.error : {};
    throw new ApiError(err.code || `HTTP_${res.status}`, err.message || `リクエストに失敗しました (${res.status})`, res.status);
  }
  return payload;
}

export function mediaUrl(kind, file) {
  // kind: 'bgm' | 'sfx' | 'raw'（すでに完全なパスの場合は raw）
  if (!file) return "";
  if (MOCK) return ""; // モックモードでは実バイト列を配信するバックエンドが無い
  if (kind === "raw") {
    if (/^https?:\/\//.test(file) || file.startsWith("/")) return file;
    return `/media/${file}`;
  }
  return `/media/${kind}/${file}`;
}

export const api = {
  mediaUrl,

  async listProjects() {
    if (MOCK) return mock.listProjects();
    return request("/api/projects");
  },

  async getProject(id) {
    if (MOCK) return mock.getProject(id);
    return request(`/api/projects/${encodeURIComponent(id)}`);
  },

  async createProject({ theme, duration, backend, style, productUrl }) {
    if (MOCK) return mock.createProject({ theme, duration, backend, style, productUrl });
    const body = { theme, duration, backend, style };
    if (productUrl) body.product_url = productUrl; // 空/未指定なら送らない（通常モードのまま）
    return request("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  async importProject(file) {
    if (MOCK) return mock.importProject(file);
    const form = new FormData();
    form.append("file", file);
    return request("/api/projects/import", { method: "POST", body: form });
  },

  async putPlan(id, plan) {
    if (MOCK) return mock.putPlan(id, plan);
    return request(`/api/projects/${encodeURIComponent(id)}/plan`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(plan),
    });
  },

  async startRender(id) {
    if (MOCK) return mock.startRender(id);
    return request(`/api/projects/${encodeURIComponent(id)}/render`, { method: "POST" });
  },

  async resumeProject(id) {
    if (MOCK) return mock.resumeProject ? mock.resumeProject(id) : mock.startRender(id);
    return request(`/api/projects/${encodeURIComponent(id)}/resume`, { method: "POST" });
  },

  async premiereExport(id) {
    // mock.js は premiereExport 未実装（実バックエンドの書き出しパッケージ生成をシミュレートしない）。
    // 存在しないジョブIDを返し、subscribeJobEvents側の「ジョブが見つかりません」経路で
    // 安全に失敗させる（クラッシュはしない）。
    if (MOCK) return mock.premiereExport ? mock.premiereExport(id) : { job_id: `mock-premiere-${id}` };
    return request(`/api/projects/${encodeURIComponent(id)}/premiere-export`, { method: "POST" });
  },

  async getBgmAssets() {
    if (MOCK) return mock.getBgmAssets();
    return request("/api/assets/bgm");
  },

  async getSfxAssets() {
    if (MOCK) return mock.getSfxAssets();
    return request("/api/assets/sfx");
  },

  // SSE 購読。 handlers: { onMessage(data), onDone(data), onError(err) }
  // 戻り値: 購読解除関数
  subscribeJobEvents(jobId, handlers) {
    if (MOCK) return mock.subscribeJobEvents(jobId, handlers);

    let closed = false;
    const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    es.onmessage = (evt) => {
      if (closed) return;
      let data;
      try { data = JSON.parse(evt.data); } catch (e) { return; }
      if (data.done) {
        handlers.onDone && handlers.onDone(data);
        es.close();
      } else {
        handlers.onMessage && handlers.onMessage(data);
      }
    };
    es.onerror = () => {
      if (closed) return;
      handlers.onError && handlers.onError(new ApiError("SSE_ERROR", "進捗の受信が切断されました"));
    };
    return () => { closed = true; es.close(); };
  },
};
