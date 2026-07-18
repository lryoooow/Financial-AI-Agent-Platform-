// 后端 API 客户端（契约两端对齐）。
// 打包版由后端同源托管前端，因此默认使用相对路径，避免固定端口失效。
// 生产路径禁止静默回退到 mock；调用方必须展示真实失败态。

import type { Candle } from "../data/market";

const API_BASE: string =
  ((import.meta as any).env?.VITE_API_BASE as string) || "";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${API_BASE}${path}`, { signal, cache: "no-store" });
  } catch (e) {
    if ((e as DOMException)?.name === "AbortError") throw e;
    throw new Error("后端连接失败，请稍后重试");
  }
  if (!r.ok) {
    let detail = "";
    try {
      const body = await r.json();
      detail = body?.detail || body?.message || body?.error || "";
    } catch {}
    throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
  }
  return (await r.json()) as T;
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    let detail = "";
    try {
      const payload = await r.json();
      detail = payload?.detail || payload?.message || payload?.error || "";
    } catch {}
    throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
  }
  return (await r.json()) as T;
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!r.ok) {
    let detail = "";
    try {
      const payload = await r.json();
      detail = payload?.detail || payload?.message || payload?.error || "";
    } catch {}
    throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
  }
  return (await r.json()) as T;
}

/** 前端所选 LLM 配置（研究/速读时随请求传后端，真正打通前后端模型）。 */
async function deleteJson<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    cache: "no-store",
  });
  if (!r.ok) {
    let detail = "";
    try {
      const payload = await r.json();
      detail = payload?.detail || payload?.message || payload?.error || "";
    } catch {}
    throw new Error(detail ? `${path} -> ${r.status}: ${detail}` : `${path} -> ${r.status}`);
  }
  return (await r.json()) as T;
}

export interface LlmConfig {
  base_url: string;
  api_key: string;
  model: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  images?: string[]; // base64 data URL（多模态图片，重开对话可恢复）
  created_at: string;
}

export interface ChatConversation {
  id: string;
  title: string;
  snapshot_interval: number;
  model_config?: LlmConfig | null;
  archived_at?: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number | null;
}

export interface ChatPayload {
  conversation: ChatConversation;
  messages: ChatMessage[];
  reply?: ChatMessage;
  channel: "deltachannel";
}

export interface Quote {
  code: string;
  name: string;
  exchange: string;
  price: number;
  change: number;
  changePct: number;
  open: number;
  high: number;
  low: number;
  prevClose: number;
  volume: string;
  turnover: string;
  marketCap: string;
  pe: number;
  pb: number;
  roe: number;
  industry: string;
  as_of?: string;
  trading_date?: string;
  elapsed_ms?: number;
  provenance?: {
    source?: string;
    endpoint?: string;
    error?: string | null;
    fetched_at?: string;
    trading_date?: string;
    note?: string;
    attempts?: { source: string; ok: boolean; error?: string }[];
  };
}

/** 行情数据 + 来源/失败原因/耗时元信息（/intraday、/kline 返回此形状）。 */
export interface MarketDataResult<T> {
  data: T;
  trading_date?: string;
  elapsed_ms?: number;
  provenance?: {
    source?: string;
    endpoint?: string;
    error?: string | null;
    fetched_at?: string;
    trading_date?: string;
    note?: string;
    attempts?: { source: string; ok: boolean; error?: string }[];
  };
}

export interface MarketSourceStatus {
  name: string;
  label: string;
  priority: number;
  status: "ok" | "fail" | "unknown";
  last_ts: string;
  error: string;
}

export interface MarketDiagnostics {
  fresh: boolean;
  market: {
    available: boolean;
    // fast 形状：按优先级排列的源状态
    sources?: MarketSourceStatus[];
    aggregated_from?: string;
    last_any_ts?: string;
    // refresh 探测形状：主/备源可达性
    primary?: { name: string; ok: boolean; error?: string | null };
    backup?: { name: string; ok: boolean; error?: string | null };
  };
  news: {
    name: string;
    available?: boolean;
    status?: string;
    last_ts?: string;
    error?: string | null;
    note?: string;
  };
  search: { name: string; configured: boolean; status: string };
  proxy: {
    bypass_enabled: boolean;
    market_proxy_set: boolean;
    no_proxy: string;
    monitored_hosts: string[];
  };
  recent: {
    kind: string;
    source: string;
    ok: boolean;
    error: string;
    ts: string;
  }[];
}

/** 联网证据状态（独立、零网络、国内优先）。 */
export interface EvidenceStatus {
  domestic_news: { name: string; available: boolean; last_checked: string; note: string };
  market_source: { name: string; last_ok: boolean; last_checked: string; source: string };
  filings: { available: boolean; note: string };
  search: { enabled: boolean; label: string; provider: string };
  summary: string;
}

/** 报告库列表项（GET /api/reports，SQLite）。 */
export interface ReportListItem {
  id: string;
  task_id: string;
  ticker: string;
  name: string;
  template: string;
  summary: string;
  as_of: string;
  created_at: string;
}

/** 研究任务状态（GET /api/research/tasks、/api/research/{id}）。 */
export interface ResearchTaskProgressEvent {
  node: string;
  label: string;
  elapsed_ms: number;
  timings?: Record<string, number>;
  ts: string;
}
export interface ResearchTaskSummary {
  id: string;
  code: string;
  template: string;
  query?: string | null;
  status: "pending" | "running" | "done" | "error";
  stage: string;
  report_id?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  progress_events?: ResearchTaskProgressEvent[];
  timings?: Record<string, unknown>;
}

/** 本地用户画像（GET/PUT /api/local-settings/user-profile）。 */
export interface UserProfile {
  risk_preference?: string;
  investment_cycle?: string;
  focus_metrics?: string;
  analysis_style?: string;
}

export interface BookEntry {
  label: string;
  price: number;
  vol: number;
}
export interface Book {
  asks: BookEntry[];
  bids: BookEntry[];
  provenance?: any;
}
export interface Indicator {
  name: string;
  value: string;
  signal: string;
}
export interface IntradayPoint {
  time: string;
  price: number;
  avg: number;
  volume: number;
}
export interface ResolveHit {
  code: string;
  name: string;
  exchange: string;
  industry: string;
  price: number;
  changePct: number;
}
export interface FinancialRow {
  name: string;
  values: string[];
}
export interface ReportDoc {
  ticker?: string;
  name?: string;
  template?: string;
  summary: string;
  catalyst?: string;
  financials: { years: string[]; rows: FinancialRow[] };
  sections: { title: string; body: string }[];
  risks: string[];
  sources: { title: string; url: string; date: string }[];
  disclaimer: string;
  as_of?: string;
  html?: string;
}

export const api = {
  base: API_BASE,

  health: () => getJson<{ status: string; db: string; llm: string }>("/api/health"),

  resolve: (q: string, limit = 8, signal?: AbortSignal) =>
    getJson<ResolveHit[]>(
      `/api/resolve?q=${encodeURIComponent(q)}&limit=${limit}`,
      signal
    ),

  quote: (code: string, signal?: AbortSignal) =>
    getJson<Quote>(`/api/quote/${encodeURIComponent(code)}`, signal),

  /** 分时：返回 {data, provenance, trading_date, elapsed_ms}（含来源/失败原因）。 */
  intraday: (code: string, signal?: AbortSignal) =>
    getJson<MarketDataResult<IntradayPoint[]>>(
      `/api/quote/${encodeURIComponent(code)}/intraday`,
      signal
    ),

  /** 日K：返回 {data, provenance, trading_date, elapsed_ms}（含来源/失败原因）。 */
  kline: (code: string, n = 60, adjust = "qfq", signal?: AbortSignal) =>
    getJson<MarketDataResult<Candle[]>>(
      `/api/quote/${encodeURIComponent(code)}/kline?n=${n}&adjust=${adjust}`,
      signal
    ),

  book: (code: string, signal?: AbortSignal) =>
    getJson<Book>(`/api/quote/${encodeURIComponent(code)}/book`, signal),

  indicators: (code: string, signal?: AbortSignal) =>
    getJson<Indicator[]>(`/api/quote/${encodeURIComponent(code)}/indicators`, signal),

  /** 行情数据源诊断：默认零网络快速（缓冲聚合）；refresh=true 现场实时探测。 */
  marketDiagnostics: (refresh = false) =>
    getJson<MarketDiagnostics>(`/api/market/diagnostics${refresh ? "?refresh=1" : ""}`),

  /** 联网证据状态（独立、零网络、国内优先）。 */
  evidenceStatus: () => getJson<EvidenceStatus>("/api/evidence/status"),

  localSetting: (key: string) =>
    getJson<{ key: string; payload: any | null }>(`/api/local-settings/${encodeURIComponent(key)}`),

  saveLocalSetting: (key: string, payload: any) =>
    putJson<{ ok: boolean; key: string }>(
      `/api/local-settings/${encodeURIComponent(key)}`,
      { payload }
    ),

  chatConversations: (archived = false) =>
    getJson<{ conversations: ChatConversation[]; channel: "deltachannel" }>(
      `/api/chat/conversations${archived ? "?archived=true" : ""}`
    ),

  createChatConversation: (title?: string, llm?: LlmConfig | null) =>
    postJson<ChatPayload>("/api/chat/conversations", {
      title,
      llm: llm ?? undefined,
      snapshot_interval: 8,
    }),

  chatConversation: (id: string) =>
    getJson<ChatPayload>(`/api/chat/conversations/${encodeURIComponent(id)}`),

  /** 流式发送对话消息（POST → text/event-stream）。用 fetch + ReadableStream 解析（EventSource 仅 GET）。
   *  事件：stage(思考阶段) → 多个 token(回复流) → done(最终 conversation/messages/reply) | error。 */
  sendChatMessageStream: async (
    id: string,
    message: string,
    llm: LlmConfig | null,
    images: string[] | undefined,
    handlers: {
      onStage?: (stage: string, label: string) => void;
      onToken?: (text: string) => void;
      onDone?: (payload: ChatPayload) => void;
      onError?: (message: string) => void;
      signal?: AbortSignal;
    }
  ) => {
    const r = await fetch(
      `${API_BASE}/api/chat/conversations/${encodeURIComponent(id)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, llm: llm ?? undefined, images: images?.length ? images : undefined }),
        signal: handlers.signal,
      }
    );
    if (!r.ok || !r.body) {
      let detail = "";
      try { const b = await r.json(); detail = b?.detail || b?.message || ""; } catch {}
      throw new Error(detail ? `chat stream → ${r.status}: ${detail}` : `chat stream → ${r.status}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop() || "";
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev: any;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === "stage") handlers.onStage?.(ev.stage, ev.label);
        else if (ev.type === "token") handlers.onToken?.(ev.text);
        else if (ev.type === "done") handlers.onDone?.(ev as ChatPayload);
        else if (ev.type === "error") handlers.onError?.(ev.message);
      }
    }
  },

  archiveChatConversation: (id: string, archived = true) =>
    putJson<{ conversation: ChatConversation; channel: "deltachannel" }>(
      `/api/chat/conversations/${encodeURIComponent(id)}/archive`,
      { archived }
    ),

  deleteChatConversation: (id: string) =>
    deleteJson<{ ok: boolean; channel: "deltachannel" }>(
      `/api/chat/conversations/${encodeURIComponent(id)}`
    ),

  /** 创建研究任务；llm 为前端所选模型配置（真正打通后端使用）。 */
  createResearch: (code: string, template = "equity_deep", query?: string, llm?: LlmConfig) =>
    postJson<{ task_id: string }>("/api/research", { code, template, query, llm }),

  /** SSE 流式研究的完整 URL（前端用 EventSource 连，带 code 兜底）。
   *  模型配置不在 URL（密钥不入 URL/日志）；后端按 task_id 从 POST 时落库/内存取出。 */
  researchStreamUrl: (taskId: string, code: string, template = "equity_deep") =>
    `${API_BASE}/api/research/${taskId}/stream?code=${encodeURIComponent(
      code
    )}&template=${template}`,

  /** 研究任务列表（恢复历史与运行中状态）。 */
  researchTasks: () => getJson<{ tasks: ResearchTaskSummary[] }>("/api/research/tasks"),

  /** 单研究任务状态（阶段/事件/耗时/报告，用于切页或重启后恢复）。 */
  researchTask: (id: string) =>
    getJson<ResearchTaskSummary>(`/api/research/${encodeURIComponent(id)}`),

  report: (id: string) =>
    getJson<ReportDoc>(`/api/reports/${encodeURIComponent(id)}`),

  /** 报告库列表（从 SQLite，重启不丢）。 */
  reportsList: () => getJson<{ reports: ReportListItem[] }>("/api/reports"),

  /** 报告真实文件下载 URL（docx/pdf/txt/md）。 */
  reportDownloadUrl: (id: string, format: "docx" | "pdf" | "txt" | "md") =>
    `${API_BASE}/api/reports/${encodeURIComponent(id)}/download?format=${format}`,

  /** 报告真删除：物理删除报告行 + 来源任务 + 检查点（不可恢复）。 */
  deleteReport: (id: string) =>
    deleteJson<{ ok: boolean; deleted: string }>(`/api/reports/${encodeURIComponent(id)}`),

  /** 本地用户画像（默认稳健研究型）。 */
  userProfile: () => getJson<{ payload: UserProfile | null }>(`/api/local-settings/user-profile`),
  saveUserProfile: (payload: UserProfile) =>
    putJson<{ payload: UserProfile }>(`/api/local-settings/user-profile`, { payload }),

  chartAnalyze: (body: {
    code: string;
    kind: "intraday" | "kline";
    point: Record<string, any>;
    prevClose: number;
    llm?: LlmConfig;
  }, signal?: AbortSignal) =>
    postJson<{ text: string; provenance: any }>("/api/chart/analyze", body, signal),

  /** 真·拉厂商 /v1/models（"拉取最新模型"按钮）；失败前端回退静态清单 */
  models: (baseUrl: string, apiKey: string) =>
    postJson<{ models: { id: string; label: string }[]; provenance: any }>(
      "/api/models",
      { base_url: baseUrl, api_key: apiKey }
    ),
};
