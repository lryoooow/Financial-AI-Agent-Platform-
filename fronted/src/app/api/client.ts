// 后端 API 客户端（契约两端对齐）。
// 打包版由后端同源托管前端，因此默认使用相对路径，避免固定端口失效。
// 生产路径禁止静默回退到 mock；调用方必须展示真实失败态。

import type { Candle } from "../data/market";

const API_BASE: string =
  ((import.meta as any).env?.VITE_API_BASE as string) || "";

function withTimeoutSignal(
  timeoutMs: number,
  externalSignal?: AbortSignal,
): { signal: AbortSignal; timedOut: () => boolean; cleanup: () => void } {
  const ac = new AbortController();
  let didTimeout = false;
  const timer = globalThis.setTimeout(() => {
    didTimeout = true;
    ac.abort(new DOMException("请求超时", "TimeoutError"));
  }, timeoutMs);

  const onExternalAbort = () => ac.abort(externalSignal?.reason);
  if (externalSignal) {
    if (externalSignal.aborted) {
      globalThis.clearTimeout(timer);
      ac.abort(externalSignal.reason);
    } else {
      externalSignal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }

  return {
    signal: ac.signal,
    timedOut: () => didTimeout,
    cleanup: () => {
      globalThis.clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);
    },
  };
}

async function parseJsonSafe<T>(r: Response, path: string): Promise<T> {
  const text = await r.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`${path} 返回格式异常`);
  }
}

function notifyAuthIfNeeded(status: number, payload?: any): void {
  if (status === 401 || payload?.code === "AUTH_REQUIRED") {
    try {
      window.dispatchEvent(new Event("jingyuan:auth-required"));
    } catch {}
  }
}

async function getJson<T>(path: string, signal?: AbortSignal, timeoutMs = 25_000): Promise<T> {
  const guard = withTimeoutSignal(timeoutMs, signal);
  let r: Response;
  try {
    r = await fetch(`${API_BASE}${path}`, { signal: guard.signal, cache: "no-store", credentials: "include" });
  } catch (e) {
    guard.cleanup();
    if (guard.timedOut()) throw new Error("请求超时，请稍后重试");
    if (signal?.aborted || (e as DOMException)?.name === "AbortError") throw e;
    throw new Error("后端连接失败，请稍后重试");
  }
  try {
    if (!r.ok) {
      let detail = "";
      try {
        const body = await r.json();
        detail = body?.detail || body?.message || body?.error || "";
        notifyAuthIfNeeded(r.status, body);
      } catch {
        notifyAuthIfNeeded(r.status);
      }
      throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
    }
    return await parseJsonSafe<T>(r, path);
  } finally {
    guard.cleanup();
  }
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal, timeoutMs = 30_000): Promise<T> {
  const guard = withTimeoutSignal(timeoutMs, signal);
  let r: Response;
  try {
    r = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: guard.signal,
      cache: "no-store",
      credentials: "include",
    });
  } catch (e) {
    guard.cleanup();
    if (guard.timedOut()) throw new Error("请求超时，请稍后重试");
    if (signal?.aborted || (e as DOMException)?.name === "AbortError") throw e;
    throw new Error("后端连接失败，请稍后重试");
  }
  try {
    if (!r.ok) {
      let detail = "";
      try {
        const payload = await r.json();
        detail = payload?.detail || payload?.message || payload?.error || "";
        notifyAuthIfNeeded(r.status, payload);
      } catch {
        notifyAuthIfNeeded(r.status);
      }
      throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
    }
    return await parseJsonSafe<T>(r, path);
  } finally {
    guard.cleanup();
  }
}

async function putJson<T>(path: string, body: unknown, timeoutMs = 30_000, method: "PUT" | "PATCH" = "PUT"): Promise<T> {
  const guard = withTimeoutSignal(timeoutMs);
  let r: Response;
  try {
    r = await fetch(`${API_BASE}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: guard.signal,
      cache: "no-store",
      credentials: "include",
    });
  } catch {
    guard.cleanup();
    if (guard.timedOut()) throw new Error("请求超时，请稍后重试");
    throw new Error("后端连接失败，请稍后重试");
  }
  try {
    if (!r.ok) {
      let detail = "";
      try {
        const payload = await r.json();
        detail = payload?.detail || payload?.message || payload?.error || "";
        notifyAuthIfNeeded(r.status, payload);
      } catch {
        notifyAuthIfNeeded(r.status);
      }
      throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
    }
    return await parseJsonSafe<T>(r, path);
  } finally {
    guard.cleanup();
  }
}

/** v1.6.3（C1）：model-configs 专用保存通道 —— 3 次退避重试 + 最终失败上抛。
 *  旧实现单发 PUT 失败被调用方 .catch(()=>{}) 静默吞掉，DB 留旧 key 而前端已持新 key，
 *  后端 overlay 会把新 key 反向覆盖回旧 key（401 持续到用户再次编辑设置）。 */
async function putJsonRetry<T>(path: string, body: unknown, retries = 3): Promise<T> {
  let lastErr: unknown = null;
  for (let i = 0; i <= retries; i++) {
    try {
      return await putJson<T>(path, body);
    } catch (e) {
      lastErr = e;
      if (i < retries) await new Promise((r) => setTimeout(r, 300 * 2 ** i));
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

/** 前端所选 LLM 配置（研究/速读时随请求传后端，真正打通前后端模型）。 */
async function deleteJson<T>(path: string, timeoutMs = 30_000): Promise<T> {
  const guard = withTimeoutSignal(timeoutMs);
  let r: Response;
  try {
    r = await fetch(`${API_BASE}${path}`, {
      method: "DELETE",
      signal: guard.signal,
      cache: "no-store",
      credentials: "include",
    });
  } catch {
    guard.cleanup();
    if (guard.timedOut()) throw new Error("请求超时，请稍后重试");
    throw new Error("后端连接失败，请稍后重试");
  }
  try {
    if (!r.ok) {
      let detail = "";
      try {
        const payload = await r.json();
        detail = payload?.detail || payload?.message || payload?.error || "";
        notifyAuthIfNeeded(r.status, payload);
      } catch {
        notifyAuthIfNeeded(r.status);
      }
      throw new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
    }
    return await parseJsonSafe<T>(r, path);
  } finally {
    guard.cleanup();
  }
}

export interface LlmConfig {
  provider_id?: string;
  base_url: string;
  api_key: string;
  model: string;
  /** 思考深度档位（low/medium/high）：仅用户在 Settings 显式选择时携带；空则不下发。 */
  reasoning_effort?: string;
  fallback_models?: string[];
}

/** 与后端 llm.py _REASONING_PATTERNS 口径一致的客户端镜像。
 *  仅供前端"看门狗档位"这类粗判使用（o 系/qwq/deepseek-r/reasoning/rt/thinking）；
 *  真正的 temperature/超时/参数下发决策一律在后端，勿在此扩展业务逻辑。 */
export function isReasoningModelLike(model: string): boolean {
  return /(?:\bo[134](?:-|\b)|gpt-5|qwq|deepseek-r|reasoning|\brt\b|thinking)/i.test(model || "");
}

/** v1.8.0：研究嵌入对话 —— 研究卡片消息携带的任务信息 */
export interface ResearchRef {
  task_id: string;
  code: string;
  template: string;
  name?: string;
  status?: "pending" | "running" | "done" | "error";
  report_id?: string | null;
  error?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  images?: string[]; // base64 data URL（多模态图片，重开对话可恢复）
  created_at: string;
  /** research_request = 研究请求气泡；research = 研究卡片（随 task 流式渲染报告） */
  kind?: "research_request" | "research" | "hitl";
  reasoning_summary?: string;
  thinking_elapsed_ms?: number;
  agent_trace?: ToolInvocation[];
  tool_invocations?: ToolInvocation[];
  artifacts?: ToolArtifact[];
  approval?: HumanApproval;
  turn_status?: "completed" | "aborted" | "awaiting_hitl" | "failed";
  research?: ResearchRef;
}

export interface KnowledgeHit {
  doc_id: string;
  kind: "report" | "conversation" | "external";
  report_id: string;
  conversation_id: string;
  title: string;
  ticker: string;
  name: string;
  template: string;
  as_of: string;
  heading: string;
  text: string;
  score: number;
}

export interface KnowledgeStats { reports: number; conversations: number; external?: number; memories?: number; chunks: number; updated_at: string; }

export interface StockHistoryItem { code: string; name: string; count: number; last_viewed: string; }

export interface LearnedProfile {
  enabled: boolean;
  stocks: { key: string; label: string; count: number }[];
  templates: { key: string; label: string; count: number }[];
  topics: { key: string; label: string; count: number }[];
  notes: { id: number; text: string; source: "model" | "user"; created_at: string }[];
}

export interface AppSettings {
  research_write_mode: "stream" | "parallel";
  default_template: "equity_deep" | "industry" | "earnings" | "risk" | "market_recap";
  context_window_k: number;
  compact_threshold_pct: number;
  knowledge_enabled: boolean;
  learn_profile: boolean;
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
  adjust?: string;
  trading_days?: number;
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
  name?: string;
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
  lots?: number;
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
  llm_degraded?: boolean;
  llm_fallback_reason?: string;
}


export interface ToolInvocation {
  call_id?: string;
  name: string;
  label: string;
  status: "start" | "ok" | "error" | "aborted";
  args_summary?: string;
  elapsed_ms?: number;
  provenance?: { source?: string; fetched_at?: string; error?: string | null } | null;
  parent_run_id?: string;
  agent_run_id?: string;
  child_run_id?: string;
}

export interface ToolArtifact {
  call_id: string;
  name: string;
  label: string;
  artifact: Record<string, unknown>;
  child_run_id?: string;
}

export interface HumanApproval {
  checkpoint_id: string;
  label: string;
  kind: "symbol" | "batch_research";
  status?: "pending" | "approved" | "rejected";
  candidates: { code: string; name: string }[];
}

export interface MarketOverview {
  indices: { code: string; name: string; price: number | null; changePct: number | null; as_of?: string }[];
  breadth: {
    up?: number; down?: number; flat?: number; count?: number; limit_up?: number | null; limit_down?: number | null;
    turnover_yuan?: number | null; previous_turnover_yuan?: number | null; turnover_change_yuan?: number | null;
    trading_date?: string; comparison_date?: string; turnover_partial?: boolean;
    provenance?: { source?: string; fetched_at?: string; error?: string | null };
  };
  industries: { code: string; name: string; changePct: number | null; net_flow_yuan: number | null; flow_provenance?: { source?: string } }[];
  industry_error?: string | null;
}

export interface WatchlistItem {
  id: string; group_id: string; symbol: string; name: string; market: string;
  added_price: number | null; note: string; sort_order: number; created_at: string;
}
export interface WatchlistGroup { id: string; name: string; sort_order: number; items: WatchlistItem[]; }
interface WatchlistChange {
  kind?: "group" | "item" | "order"; id?: string; group_id?: string; name?: string;
  symbol?: string; symbols?: string[]; ids?: string[]; note?: string; sort_order?: number;
}
export interface WatchlistPreview {
  verified: { code: string; name: string; input: string }[];
  ambiguous: { input: string; candidates: { code: string; name: string }[] }[];
  unmatched: string[];
}
type EvidenceSource = { source?: string; endpoint?: string; fetched_at?: string; error?: string };
export interface RadarRow {
  symbol: string; name: string; quote: Partial<Quote>; groups: string[];
  valuation: { pe_ttm?: number; pb?: number; pe_ttm_percentile?: number; pb_percentile?: number; as_of?: string; provenance?: EvidenceSource };
  earnings: { period?: string; revenue_yoy?: number; profit_yoy?: number; roe?: number; operating_cashflow?: number; provenance?: EvidenceSource; cashflow_provenance?: EvidenceSource };
  price_volume: { close?: number; ma20?: number; rsi?: number; volume_ratio?: number; as_of?: string; provenance?: EvidenceSource };
  risk: { volatility_20d?: number; max_drawdown?: number; beta?: number; as_of?: string; provenance?: { asset?: EvidenceSource; benchmark?: EvidenceSource } };
  events: { count?: number; items: { title?: string; time?: string; url?: string }[]; provenance?: EvidenceSource };
}
interface BatchOutcome {
  status: "awaiting_hitl" | "queued" | "rejected";
  approval?: HumanApproval;
  conversation_id?: string;
  task_ids?: string[];
}
export interface FactSnapshot {
  report_price?: number | null; report_pe_ttm?: number | null; report_pb?: number | null;
  price_as_of?: string; valuation_as_of?: string; snapshot_ts?: string; hs300_as_of?: string; hs300_close?: number | null;
  fin_period?: string; revenue_yoy?: number | null; profit_yoy?: number | null;
  provenance?: Record<string, EvidenceSource | null>;
}
export interface ReportLedger {
  snapshot: FactSnapshot; current: FactSnapshot; legacy: boolean; message?: string;
  comparison: { price_change_pct?: number; excess_pp?: number; max_drawdown_pct?: number; pe_change_pct?: number; pb_change_pct?: number;
    new_financial_period?: boolean; return_start?: string; return_end?: string; drawdown_as_of?: string };
}
export interface KnowledgeDocument { id: string; title: string; ticker: string; created_at: string; chunks: number; }
export type PushChannel = "wework" | "feishu" | "dingtalk" | "smtp";
export interface PushConfig {
  enabled?: boolean; webhook?: string; secret?: string; host?: string; port?: number;
  tls?: "ssl" | "starttls"; username?: string; password?: string; sender?: string; recipient?: string;
}
export interface SchedulerConfig {
  enabled: boolean; run_time: string; group_id: string; attach_pdf: boolean; channels: Partial<Record<PushChannel, PushConfig>>;
  runs?: { id: string; date: string; status: string; report_id?: string; channels?: Record<string, { ok: boolean }>; error?: string }[];
}
export interface McpServer { id: string; command: string[]; env: Record<string, string>; allowed_tools: string[]; enabled: boolean; }
export type ReportFormat = "docx" | "pdf" | "txt" | "md" | "html";

interface ChatHandlers {
  onStage?: (stage: string, label: string, heartbeat?: boolean) => void;
  onToolCall?: (event: ToolInvocation) => void;
  onArtifact?: (event: ToolArtifact) => void;
  onApproval?: (event: HumanApproval) => void;
  onReasoning?: (text: string) => void;
  onToken?: (text: string) => void;
  onCompacted?: (info: { generation: number; used_tokens: number; window: number }) => void;
  onDone?: (payload: ChatPayload) => void;
  onError?: (message: string) => void;
  signal?: AbortSignal;
}

async function readChatResponse(r: Response, handlers: ChatHandlers) {
  if (!r.ok || !r.body) {
    let detail = "";
    try {
      const value = await r.json();
      detail = typeof value.detail === "string" ? value.detail : value.detail?.message || "";
      notifyAuthIfNeeded(r.status, value);
    } catch {
      notifyAuthIfNeeded(r.status);
    }
    throw new Error(detail || "会话请求失败（HTTP " + r.status + "）");
  }
  if (!r.headers.get("Content-Type")?.includes("text/event-stream")) return;
  const reader = r.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let terminal = false;
  const process = (frame: string) => {
    const line = frame.split("\n").find((part) => part.startsWith("data: "));
    if (!line) return;
    let event;
    try { event = JSON.parse(line.slice(6)); } catch { return; }
    switch (event.type) {
      case "stage": handlers.onStage?.(event.stage, event.label, event.heartbeat === true); break;
      case "tool_call": handlers.onToolCall?.(event); break;
      case "tool_artifact": handlers.onArtifact?.(event); break;
      case "human_approval_required": handlers.onApproval?.(event); break;
      case "reasoning": handlers.onReasoning?.(event.text); break;
      case "token": handlers.onToken?.(event.text); break;
      case "context_compacted": handlers.onCompacted?.(event); break;
      case "done": terminal = true; handlers.onDone?.(event); break;
      case "error": terminal = true; handlers.onError?.(event.message); break;
    }
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.replace(/\r\n/g, "\n").split("\n\n");
      buffer = done ? "" : frames.pop() || "";
      for (const frame of frames) process(frame);
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
  if (!terminal && !handlers.signal?.aborted) handlers.onError?.("对话流连接意外中断，请重试。");
}

async function chatRequest(path: string, payload: unknown, handlers: ChatHandlers) {
  const response = await fetch(API_BASE + path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), signal: handlers.signal, cache: "no-store", credentials: "include",
  });
  await readChatResponse(response, handlers);
}

export const api = {
  base: API_BASE,

  health: () => getJson<{ status: string; db: string; llm: string }>("/api/health"),
  watchlist: () => getJson<{ groups: WatchlistGroup[] }>("/api/watchlist"),
  addWatchlist: (body: WatchlistChange) => postJson<{ id?: string; items?: WatchlistItem[]; duplicates?: number }>("/api/watchlist", body, undefined, 90_000),
  updateWatchlist: (body: WatchlistChange) => putJson<{ ok: boolean }>("/api/watchlist", body, 30_000, "PATCH"),
  removeWatchlist: (id: string, kind: "item" | "group" = "item") =>
    deleteJson<{ ok: boolean }>("/api/watchlist?id=" + encodeURIComponent(id) + "&kind=" + kind),
  watchlistQuotes: (id: string, signal?: AbortSignal) =>
    getJson<{ quotes: Record<string, Partial<Quote>> }>("/api/watchlist/quotes?group_id=" + encodeURIComponent(id), signal, 70_000),
  watchlistPreview: (body: { mode: "text" | "table" | "image"; payload: string; filename?: string; llm?: LlmConfig }) =>
    postJson<WatchlistPreview>("/api/watchlist/import-preview", body, undefined, 48_000),
  watchlistRadar: (group_id: string, signal?: AbortSignal) =>
    postJson<{ rows: RadarRow[] }>("/api/watchlist/radar", { group_id }, signal, 320_000),
  batchResearch: (symbols: string[], llm?: LlmConfig) =>
    postJson<BatchOutcome>("/api/research/batch", { symbols, llm }).then((res) => {
      if (res.task_ids?.length) {
        try { window.dispatchEvent(new CustomEvent("finresearch:research-created", { detail: res })); } catch {}
      }
      return res;
    }),
  approveBatch: (conversationId: string, checkpoint_id: string, action: "approve" | "edit" | "reject", value = "") =>
    postJson<BatchOutcome>("/api/chat/" + encodeURIComponent(conversationId) + "/hitl-resume", { checkpoint_id, action, value }).then((res) => {
      if (res.task_ids?.length) {
        try { window.dispatchEvent(new CustomEvent("finresearch:research-created", { detail: res })); } catch {}
      }
      return res;
    }),
  ledger: (id: string, signal?: AbortSignal) => getJson<ReportLedger>("/api/reports/" + encodeURIComponent(id) + "/ledger", signal, 35_000),
  knowledgeDocuments: () => getJson<{ items: KnowledgeDocument[] }>("/api/knowledge/documents"),
  knowledgeDeleteDocument: (id: string) => deleteJson<{ ok: boolean }>("/api/knowledge/documents/" + encodeURIComponent(id)),
  knowledgeUpload: async (file: File) => {
    const body = new FormData();
    body.set("file", file);
    const response = await fetch(API_BASE + "/api/knowledge/upload", {
      method: "POST", body, credentials: "include", signal: AbortSignal.timeout(40_000),
    });
    const data = await parseJsonSafe<{ detail?: string; ok?: boolean; code?: string }>(response, "/api/knowledge/upload");
    if (!response.ok) {
      notifyAuthIfNeeded(response.status, data);
      throw new Error(data.detail || "上传失败。");
    }
    return data;
  },
  schedulerConfig: () => getJson<SchedulerConfig>("/api/scheduler/config"),
  saveScheduler: (body: SchedulerConfig) => putJson<SchedulerConfig>("/api/scheduler/config", body),
  testPush: (channel: PushChannel) => postJson<{ ok: boolean; error?: string }>("/api/scheduler/test-push", { channel }),
  mcpServers: () => getJson<{ servers: McpServer[]; editable?: boolean }>("/api/mcp/servers"),
  saveMcpServers: (servers: McpServer[]) => putJson<{ ok: boolean }>("/api/mcp/servers", { servers }),

  resolve: (q: string, limit = 8, signal?: AbortSignal) =>
    getJson<ResolveHit[]>(
      `/api/resolve?q=${encodeURIComponent(q)}&limit=${limit}`,
      signal
    ),

  marketOverview: (signal?: AbortSignal, refresh = false) =>
    getJson<MarketOverview>("/api/market/overview?refresh=" + Number(refresh), signal),

  webAuthStatus: () => getJson<{ required: boolean; authenticated: boolean }>("/api/auth/status"),
  webAuthLogin: (token: string) => postJson<{ authenticated: boolean }>("/api/auth/login", { token }),
  webAuthLogout: () => postJson<{ authenticated: boolean }>("/api/auth/logout", {}),

  quote: (code: string, signal?: AbortSignal) =>
    getJson<Quote>(`/api/quote/${encodeURIComponent(code)}`, signal),

  /** 分时：返回 {data, provenance, trading_date, elapsed_ms}（含来源/失败原因）。 */
  intraday: (code: string, signal?: AbortSignal, days = 1) =>
    getJson<MarketDataResult<IntradayPoint[]>>(
      `/api/quote/${encodeURIComponent(code)}/intraday?days=${days}`,
      signal
    ),

  /** 日K：返回 {data, provenance, trading_date, elapsed_ms}（含来源/失败原因）。 */
  kline: (code: string, n = 60, adjust = "qfq", signal?: AbortSignal, period = "daily") =>
    getJson<MarketDataResult<Candle[]>>(
      `/api/quote/${encodeURIComponent(code)}/kline?n=${n}&adjust=${encodeURIComponent(adjust)}&period=${period}`,
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
    // v1.6.3（C1）：密钥配置走重试通道（保存失败绝不能静默 —— DB 是后端 overlay 的
    // 事实源，旧 key 留库会把前端新 key 反向覆盖回 401）
    key === "model-configs"
      ? putJsonRetry<{ ok: boolean; key: string }>(
          `/api/local-settings/${encodeURIComponent(key)}`,
          { payload }
        )
      : putJson<{ ok: boolean; key: string }>(
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
  sendChatMessageStream: (
    id: string, message: string, llm: LlmConfig | null, images: string[] | undefined,
    handlers: ChatHandlers, activeSymbol?: string,
  ) => chatRequest("/api/chat/conversations/" + encodeURIComponent(id) + "/messages", {
    message, llm: llm ?? undefined, images: images?.length ? images : undefined, active_symbol: activeSymbol,
  }, handlers),

  resumeChat: (
    id: string, checkpointId: string, action: "approve" | "edit" | "reject", value: string, handlers: ChatHandlers,
  ) => chatRequest("/api/chat/" + encodeURIComponent(id) + "/hitl-resume", {
    checkpoint_id: checkpointId, action, value,
  }, handlers),

  archiveChatConversation: (id: string, archived = true) =>
    putJson<{ conversation: ChatConversation; channel: "deltachannel" }>(
      `/api/chat/conversations/${encodeURIComponent(id)}/archive`,
      { archived }
    ),

  deleteChatConversation: (id: string) =>
    deleteJson<{ ok: boolean; channel: "deltachannel" }>(
      `/api/chat/conversations/${encodeURIComponent(id)}`
    ),

  /** 创建研究任务；llm 为前端所选模型配置（真正打通后端使用）。
   *  v1.8.0：带 conversationId 时研究嵌入该对话，返回追加的两条消息（研究请求 + 研究卡片）。 */
  createResearch: (
    code: string,
    template = "equity_deep",
    query?: string,
    llm?: LlmConfig,
    opts?: { conversationId?: string; name?: string },
  ) =>
    postJson<{ task_id: string; reused?: boolean; conversation?: ChatConversation; messages?: ChatMessage[] }>(
      "/api/research",
      { code, template, query, llm, conversation_id: opts?.conversationId, name: opts?.name },
    ).then((res) => {
      try {
        window.dispatchEvent(
          new CustomEvent("finresearch:research-created", {
            detail: { ...res, code, template, query, name: opts?.name },
          }),
        );
      } catch {}
      return res;
    }),

  /** SSE 流式研究的完整 URL（前端用 EventSource 连，带 code 兜底）。
   *  模型配置不在 URL（密钥不入 URL/日志）；后端按 task_id 从 POST 时落库/内存取出。 */
  researchStreamUrl: (taskId: string, code: string, template = "equity_deep") =>
    `${API_BASE}/api/research/${encodeURIComponent(taskId)}/stream?code=${encodeURIComponent(
      code
    )}&template=${encodeURIComponent(template)}`,

  /** 研究任务列表（恢复历史与运行中状态）。 */
  researchTasks: () => getJson<{ tasks: ResearchTaskSummary[] }>("/api/research/tasks"),

  /** 单研究任务状态（阶段/事件/耗时/报告，用于切页或重启后恢复）。 */
  researchTask: (id: string) =>
    getJson<ResearchTaskSummary>(`/api/research/${encodeURIComponent(id)}`),

  /** 主动取消正在运行的研究任务。 */
  cancelResearch: (id: string) =>
    postJson<{ ok: boolean; task_id: string; status: string }>(
      `/api/research/${encodeURIComponent(id)}/cancel`,
      {},
    ),

  /** 删除未生成报告的失败/已取消研究任务记录及检查点。 */
  deleteResearchTask: (id: string) =>
    deleteJson<{ ok: boolean; deleted: string }>(`/api/research/${encodeURIComponent(id)}`),

  report: (id: string) =>
    getJson<ReportDoc>(`/api/reports/${encodeURIComponent(id)}`),

  /** 报告库列表（从 SQLite，重启不丢）。 */
  reportsList: () => getJson<{ reports: ReportListItem[] }>("/api/reports"),

  /** 报告真实文件下载 URL（docx/pdf/txt/md）。 */
  reportDownloadUrl: (id: string, format: ReportFormat) =>
    `${API_BASE}/api/reports/${encodeURIComponent(id)}/download?format=${format}`,

  /** 报告真删除：物理删除报告行 + 来源任务 + 检查点（不可恢复）。 */
  deleteReport: (id: string) =>
    deleteJson<{ ok: boolean; deleted: string; task_id?: string | null }>(`/api/reports/${encodeURIComponent(id)}`).then((res) => {
      try {
        window.dispatchEvent(new CustomEvent("finresearch:report-deleted", { detail: { id, taskId: res.task_id ?? undefined } }));
      } catch {}
      return res;
    }),

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

  // ── v1.8.0：知识库 / 画像 / 搜索记录 / 应用偏好 ──
  knowledgeSearch: (q: string, opts?: { limit?: number; ticker?: string; kind?: string }) =>
    getJson<{ items: KnowledgeHit[] }>(
      `/api/knowledge/search?q=${encodeURIComponent(q)}&limit=${opts?.limit ?? 8}` +
        (opts?.ticker ? `&ticker=${encodeURIComponent(opts.ticker)}` : "") +
        (opts?.kind ? `&kind=${encodeURIComponent(opts.kind)}` : ""),
    ),
  knowledgeStats: () => getJson<KnowledgeStats>("/api/knowledge/stats"),
  knowledgeReindex: () => postJson<KnowledgeStats & { ok: boolean; indexed: number }>("/api/knowledge/reindex", {}),

  marketHistory: (limit = 8) =>
    getJson<{ recent: StockHistoryItem[]; frequent: StockHistoryItem[] }>(`/api/market/history?limit=${limit}`),
  recordStockView: (code: string, name?: string) =>
    postJson<{ ok: boolean }>("/api/market/history", { code, name }),
  clearMarketHistory: () => deleteJson<{ ok: boolean; deleted: number }>("/api/market/history"),

  learnedProfile: () => getJson<LearnedProfile>("/api/profile/learned"),
  clearLearnedProfile: () => deleteJson<{ ok: boolean }>("/api/profile/learned"),
  addProfileNote: (text: string) =>
    postJson<{ id: number; text: string; source: string; created_at: string }>("/api/profile/notes", { text }),
  deleteProfileNote: (id: number) => deleteJson<{ ok: boolean }>(`/api/profile/notes/${id}`),

  appSettings: () => getJson<AppSettings>("/api/app-settings"),
  saveAppSettings: (payload: AppSettings) =>
    putJson<{ ok: boolean; key: string }>("/api/local-settings/app-settings", { payload }),

  /** 真·拉厂商 /v1/models（"拉取最新模型"按钮）；失败前端回退静态清单 */
  models: (baseUrl: string, apiKey: string, providerId?: string) =>
    postJson<{ models: { id: string; label: string }[]; provenance: any }>(
      "/api/models",
      { base_url: baseUrl, api_key: apiKey, provider_id: providerId }
    ),
};

/** 桌面/浏览器通用报告下载（fetch + Blob + 延迟释放 ObjectURL，兼容 Electron will-navigate 拦截）。 */
export async function downloadReportFile(
  id: string,
  format: ReportFormat,
): Promise<void> {
  const guard = withTimeoutSignal(90_000);
  let r: Response;
  try {
    r = await fetch(api.reportDownloadUrl(id, format), {
      signal: guard.signal,
      cache: "no-store",
      credentials: "include",
    });
  } catch {
    guard.cleanup();
    if (guard.timedOut()) throw new Error("导出超时，请稍后重试");
    throw new Error("后端连接失败，请稍后重试");
  }
  try {
    if (!r.ok) {
      let detail = "";
      try {
        const b = await r.json();
        detail = b?.detail || b?.message || "";
        notifyAuthIfNeeded(r.status, b);
      } catch {
        notifyAuthIfNeeded(r.status);
      }
      throw new Error(detail ? `导出失败（${r.status}）：${detail}` : `导出失败（HTTP ${r.status}）`);
    }
    const cd = r.headers.get("content-disposition") || "";
    const mUtf = /filename\*=UTF-8''([^;\n]+)/i.exec(cd);
    const mPlain = /filename="?([^";\n]+)"?/i.exec(cd);
    const filename = mUtf
      ? decodeURIComponent(mUtf[1])
      : mPlain
        ? mPlain[1]
        : `report_${id}.${format}`;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 预留充足时间供用户在系统「另存为」对话框中选择目录（10s 在慢盘/网络盘下易提前失效）
    setTimeout(() => URL.revokeObjectURL(url), 120_000);
  } finally {
    guard.cleanup();
  }
}
