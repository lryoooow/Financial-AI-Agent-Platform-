import { useEffect, useRef, useState, type PointerEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowUpRight, ArrowDownRight, TrendingUp, Search, Loader2, AlertTriangle, Database, Globe2, ShieldCheck, RefreshCw, History, Star, Trash2 } from "lucide-react";
import type { Stock, Candle } from "../../data/market";
import { api, type Book, type Indicator, type IntradayPoint, type MarketDiagnostics, type MarketOverview, type Quote, type ResolveHit, type StockHistoryItem } from "../../api/client";
import { AnimatedNumber } from "../AnimatedNumber";
import { OrderBook } from "../OrderBook";
import { IntradayChart } from "../charts/IntradayChart";
import { KLineChart } from "../charts/KLineChart";
import { fmt, signed, trendClass } from "../../lib/format";
import { useConfirm } from "../ui/confirm";
import { Skeleton } from "../ui/skeleton";
import { WatchlistPanel } from "../watchlist/WatchlistPanel";

const EMPTY_BOOK: Book = { asks: [], bids: [] };
const LEFT_WIDTH_KEY = "finresearch_market_left_width";

const signalColor: Record<string, string> = {
  多头: "var(--up)",
  金叉: "var(--up)",
  超买: "var(--up)",
  死叉: "var(--down)",
  空头: "var(--down)",
  超卖: "var(--down)",
  中性: "var(--muted-foreground)",
};

const CST_MARKET_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai",
  hour12: false,
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});

function isCstTradingHours(): boolean {
  const parts = CST_MARKET_FMT.formatToParts(new Date());
  const m: Record<string, string> = {};
  for (const p of parts) m[p.type] = p.value;
  const wk = m.weekday ?? "";
  if (!["Mon", "Tue", "Wed", "Thu", "Fri"].includes(wk)) return false;
  const mins = (Number(m.hour ?? 0) % 24) * 60 + Number(m.minute ?? 0);
  return (mins >= 565 && mins <= 695) || (mins >= 778 && mins <= 905);
}

function fallbackMeta(code: string): Pick<Stock, "code" | "name" | "exchange" | "industry"> {
  return {
    code,
    name: code,
    exchange: code.split(".")[1] || "A股",
    industry: "",
  };
}

function isUsableQuote(q: Quote | null): q is Quote {
  return !!q && !q.provenance?.error && Number.isFinite(q.price) && q.price > 0;
}

function DataUnavailable({ text }: { text: string }) {
  return (
    <div className="h-full grid place-items-center text-center px-6">
      <div className="max-w-sm">
        <div className="mx-auto grid place-items-center size-10 rounded-lg bg-destructive/10 mb-3">
          <AlertTriangle className="size-5 text-destructive" />
        </div>
        <div className="text-sm text-foreground">真实数据不可用</div>
        <p className="text-xs text-muted-foreground mt-1 leading-relaxed">{text}</p>
      </div>
    </div>
  );
}

function DiagnosticsCard({
  diagnostics,
  error,
  onRefresh,
  refreshing,
}: {
  diagnostics: MarketDiagnostics | null;
  error: string;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const sources = diagnostics?.market.sources;
  const marketOk = diagnostics?.market.available ?? false;
  const probeShape = !sources && diagnostics?.market.primary; // 手动刷新返回的探测形状
  const recent = diagnostics?.recent?.slice(0, 3) ?? [];
  const dot = (st?: string) => st === "ok" ? "var(--down)" : st === "fail" ? "var(--up)" : "var(--muted-foreground)";

  return (
    <div className="rounded-xl border border-border bg-card p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2">
          <Database className={`size-4 ${marketOk ? "text-down" : "text-muted-foreground"}`} />
          <span className="text-sm text-foreground">数据源状态</span>
          {diagnostics && <span className={`text-[10px] px-1.5 py-0.5 rounded ${marketOk ? "bg-down/10 text-down" : "bg-up/10 text-up"}`}>{marketOk ? "可用" : "待检查"}</span>}
        </div>
        <button onClick={onRefresh} disabled={refreshing} className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/70 disabled:opacity-50 transition-colors">
          {refreshing ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
          刷新
        </button>
      </div>

      {error ? (
        <p className="text-[11px] leading-relaxed text-muted-foreground">诊断接口不可用：{error}</p>
      ) : sources ? (
        <div className="space-y-1 text-[11px]">
          {sources.map((s) => (
            <div key={s.name} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="size-1.5 rounded-full shrink-0" style={{ background: dot(s.status) }} />
                <span className="text-muted-foreground truncate">{s.priority}. {s.label}</span>
              </span>
              <span className={`shrink-0 ${s.status === "ok" ? "text-down" : s.status === "fail" ? "text-up" : "text-muted-foreground"}`}>
                {s.status === "ok" ? "可用" : s.status === "fail" ? (s.error?.slice(0, 24) || "失败") : "未探测"}
              </span>
            </div>
          ))}
          <div className="flex items-center justify-between pt-1 mt-1 border-t border-border">
            <span className="flex items-center gap-1.5 text-muted-foreground"><ShieldCheck className="size-3.5" /> 代理绕过</span>
            <span className={diagnostics?.proxy.bypass_enabled ? "text-down" : "text-up"}>{diagnostics?.proxy.bypass_enabled ? "已开启" : "未开启"}</span>
          </div>
        </div>
      ) : probeShape ? (
        <div className="space-y-1 text-[11px]">
          <div className="flex items-center justify-between"><span className="text-muted-foreground">主源（AkShare 探测）</span><span className={diagnostics?.market.primary?.ok ? "text-down" : "text-up"}>{diagnostics?.market.primary?.ok ? "可达" : "失败"}</span></div>
          <div className="flex items-center justify-between"><span className="text-muted-foreground">备用（Sina 探测）</span><span className={diagnostics?.market.backup?.ok ? "text-down" : "text-up"}>{diagnostics?.market.backup?.ok ? "可达" : "失败"}</span></div>
        </div>
      ) : (
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground"><Loader2 className="size-3.5 animate-spin" /> 正在读取数据源状态</div>
      )}

      {recent.length > 0 && (
        <div className="mt-2 pt-2 border-t border-border space-y-0.5 text-[10.5px] text-muted-foreground/80">
          {recent.map((r, i) => (
            <div key={`${r.ts}-${i}`} className="leading-relaxed flex items-center gap-1">
              <span className={r.ok ? "text-down" : "text-up"}>{r.source}</span>
              <span>· {r.kind}</span>
              <span className="truncate">{r.ok ? "OK" : r.error}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function MarketView({
  code,
  onSelect,
  onResearch,
  visible = true,
  indices,
}: {
  code: string;
  onSelect: (c: string) => void;
  onResearch: (c: string, name: string) => void;
  /** 页面常驻挂载（hidden 切页）：回到前台时刷新搜索记录（其间研究也会计入） */
  visible?: boolean;
  indices?: MarketOverview["indices"];
}) {
  const confirm = useConfirm();
  // v1.8.0：搜索记录（最近查看 + 最常查看），成功加载行情即记一次查看
  const [history, setHistory] = useState<{ recent: StockHistoryItem[]; frequent: StockHistoryItem[] }>({ recent: [], frequent: [] });
  const [historyTab, setHistoryTab] = useState<"recent" | "frequent">("recent");
  const loadHistory = () => api.marketHistory(10).then(setHistory).catch(() => {});
  useEffect(() => { if (visible) void loadHistory(); }, [visible]);
  const meta = fallbackMeta(code);
  const [stock, setStock] = useState<Quote | null>(null);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const [tab, setTab] = useState<"intraday" | "kline">("intraday");
  const [days, setDays] = useState(1);
  const [period, setPeriod] = useState<"daily" | "weekly" | "monthly">("daily");
  const [adjust, setAdjust] = useState("qfq");
  const [actualAdjust, setActualAdjust] = useState<string>();
  const [tradingDays, setTradingDays] = useState<number>();
  const chartKey = [code, days, period, adjust].join("|");
  const lastChartRef = useRef("");
  const [q, setQ] = useState("");

  const [intraday, setIntraday] = useState<IntradayPoint[]>([]);
  const [kline, setKline] = useState<Candle[]>([]);
  const [book, setBook] = useState<Book>(EMPTY_BOOK);
  const [indicators, setIndicators] = useState<Indicator[]>([]);
  const [dataAsOf, setDataAsOf] = useState("");
  const [chartError, setChartError] = useState("");
  // 分时/日K 的来源元信息（哪个源、哪些源失败、原因、耗时）—— 前端据此显示"腾讯源成功 / AkShare 失败已切换"
  const [intradayMeta, setIntradayMeta] = useState<{ source?: string; attempts?: { source: string; ok: boolean; error?: string }[]; trading_date?: string; elapsed_ms?: number } | null>(null);
  const [klineMeta, setKlineMeta] = useState<{ source?: string; attempts?: { source: string; ok: boolean; error?: string }[]; trading_date?: string; elapsed_ms?: number } | null>(null);
  const [diagnostics, setDiagnostics] = useState<MarketDiagnostics | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState("");
  const [diagRefreshing, setDiagRefreshing] = useState(false);

  const [results, setResults] = useState<ResolveHit[] | null>(null);
  const [searchError, setSearchError] = useState(false);
  const [searching, setSearching] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [silentRefreshing, setSilentRefreshing] = useState(false);
  const lastCodeRef = useRef("");
  const loadSeq = useRef(0);
  const [leftWidth, setLeftWidth] = useState(() => {
    try {
      const saved = Number(window.localStorage.getItem(LEFT_WIDTH_KEY));
      return Number.isFinite(saved) && saved > 0 ? Math.min(380, Math.max(180, saved)) : 220;
    } catch {
      return 220;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(LEFT_WIDTH_KEY, String(Math.round(leftWidth)));
    } catch {}
  }, [leftWidth]);

  const startLeftResize = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = leftWidth;
    const previousCursor = document.body.style.cursor;
    const previousSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (moveEvent: globalThis.PointerEvent) => {
      const next = startWidth + moveEvent.clientX - startX;
      setLeftWidth(Math.min(380, Math.max(180, next)));
    };
    const onUp = () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousSelect;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointercancel", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
    // v1.6.3（E）：手势被系统取消（触摸滚动接管/Alt+Tab）时同 pointerup 清理
    //（旧实现 pointerup 一次性监听不会触发 → move 监听与 body 光标锁死泄漏）
    window.addEventListener("pointercancel", onUp, { once: true });
  };

  // 交易时段前台自动静默刷新（每 15 秒更新报价/分时/五档，不打断看图）
  useEffect(() => {
    if (!visible) return;
    const t = window.setInterval(() => {
      if (document.hidden || !isCstTradingHours()) return;
      setRefreshTick((n) => n + 1);
    }, 15_000);
    return () => window.clearInterval(t);
  }, [visible]);

  useEffect(() => {
    if (!visible) return;
    const seq = ++loadSeq.current;
    const ac = new AbortController();
    let cancelled = false;
    const isCodeSwitch = lastCodeRef.current !== code;
    const changedChart = lastChartRef.current !== chartKey;
    if (changedChart) {
      setChartError("");
      setIntraday([]);
      setKline([]);
      setIntradayMeta(null);
      setKlineMeta(null);
      setActualAdjust(undefined);
      setTradingDays(undefined);
    }
    if (isCodeSwitch) {
      setQuoteLoading(true);
      setQuoteError("");
      setChartError("");
      setStock(null);
      setDataAsOf("");
      setIntraday([]);
      setKline([]);
      setBook(EMPTY_BOOK);
      setIndicators([]);
      setIntradayMeta(null);
      setKlineMeta(null);
    } else {
      setSilentRefreshing(true);
    }

    const timer = window.setTimeout(() => {
      lastCodeRef.current = code;
      lastChartRef.current = chartKey;
      (async () => {
      try {
        const qq = await api.quote(code, ac.signal);
        if (cancelled || ac.signal.aborted || seq !== loadSeq.current) return;
        if (!isUsableQuote(qq)) {
          // 类型守卫的负分支把 qq 收窄成 null；显式宽化回 Quote|null 再取失败原因
          const reason = (qq as Quote | null)?.provenance?.error || "行情接口未返回有效价格";
          if (isCodeSwitch) setQuoteError(reason);
          return;
        }
        setQuoteError("");
        setStock(qq);
        setDataAsOf(qq.as_of || qq.provenance?.fetched_at || "");
        if (isCodeSwitch) api.recordStockView(code, qq.name).then(loadHistory).catch(() => {});

        const [intraRes, klineRes, bookRes, indRes] = await Promise.allSettled([
          api.intraday(code, ac.signal, days),
          api.kline(code, 90, adjust, ac.signal, period),
          api.book(code, ac.signal),
          api.indicators(code, ac.signal),
        ]);
        if (cancelled || ac.signal.aborted || seq !== loadSeq.current) return;

        if (intraRes.status === "fulfilled") {
          const v = intraRes.value;
          if ((v.data || []).length || changedChart) setIntraday(v.data || []);
          setTradingDays(v.trading_days);
          setIntradayMeta({ source: v.provenance?.source, attempts: v.provenance?.attempts, trading_date: v.trading_date, elapsed_ms: v.elapsed_ms });
        }
        if (klineRes.status === "fulfilled") {
          const v = klineRes.value;
          if ((v.data || []).length || changedChart) setKline(v.data || []);
          setActualAdjust(v.adjust);
          setKlineMeta({ source: v.provenance?.source, attempts: v.provenance?.attempts, trading_date: v.trading_date, elapsed_ms: v.elapsed_ms });
        }
        if (bookRes.status === "fulfilled") setBook(bookRes.value);
        if (indRes.status === "fulfilled" && indRes.value.length) setIndicators(indRes.value);

        if (isCodeSwitch) {
          const missing: string[] = [];
          if (intraRes.status !== "fulfilled" || !(intraRes.value.data || []).length) missing.push("分时");
          if (klineRes.status !== "fulfilled" || !(klineRes.value.data || []).length) missing.push("K线");
          if (missing.length) setChartError(`${missing.join("、")}未返回真实数据，请检查数据源或稍后重试。`);
        }
      } catch (e) {
        if (!cancelled && !ac.signal.aborted && seq === loadSeq.current && isCodeSwitch) {
          setQuoteError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled && !ac.signal.aborted && seq === loadSeq.current) {
          setQuoteLoading(false);
          setSilentRefreshing(false);
        }
      }
    })();
    }, isCodeSwitch ? 120 : 0);

    return () => {
      cancelled = true;
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [code, refreshTick, chartKey, days, period, adjust, visible]);

  // 诊断：仅挂载时读一次 fast（零网络）；不再随 code/quoteError 重探（避免重试风暴）。手动刷新才探测。
  useEffect(() => {
    let cancelled = false;
    setDiagnosticsError("");
    api.marketDiagnostics(false)
      .then((d) => { if (!cancelled) setDiagnostics(d); })
      .catch((e) => { if (!cancelled) setDiagnosticsError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, []);

  const refreshDiagnostics = async () => {
    setDiagRefreshing(true);
    try {
      await api.marketDiagnostics(true);   // 现场实时探测，结果回写环形缓冲
      const d = await api.marketDiagnostics(false); // 再读 fast（已含最新状态）
      setDiagnostics(d);
      setDiagnosticsError("");
    } catch (e) {
      setDiagnosticsError(e instanceof Error ? e.message : String(e));
    } finally {
      setDiagRefreshing(false);
    }
  };

  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setResults(null);
      setSearchError(false);
      setSearching(false);
      return;
    }
    setSearching(true);
    setSearchError(false);
    let cancelled = false;
    const ac = new AbortController();
    const t = setTimeout(async () => {
      try {
        const r = await api.resolve(query, 12, ac.signal);
        if (!cancelled) {
          setResults(Array.isArray(r) ? r : []);
          setSearchError(false);
        }
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") return;
        // 服务错误独立由 searchError 标识，results 严格保持数组类型，杜绝 list.map 崩溃
        if (!cancelled) {
          setSearchError(true);
          setResults([]);
        }
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      ac.abort();
      clearTimeout(t);
    };
  }, [q]);

  const display = stock ?? ({
    ...meta,
    price: 0,
    change: 0,
    changePct: 0,
    open: 0,
    high: 0,
    low: 0,
    prevClose: 0,
    volume: "—",
    turnover: "—",
    marketCap: "—",
    pe: 0,
    pb: 0,
    roe: 0,
  } as Quote);

  // 当前图（分时/日K）的数据源 + 失败原因（显示"腾讯源成功 / AkShare 失败已切换"）
  const activeMeta = tab === "intraday" ? intradayMeta : klineMeta;
  const chartSrc = activeMeta?.source;
  const chartSourceLabel = chartSrc === "sina" ? "新浪直连" : chartSrc === "tencent" ? "腾讯直连" : chartSrc === "eastmoney-direct" ? "东财直连" : chartSrc === "akshare" ? "AkShare兜底" : chartSrc ? chartSrc : "";
  const failedAttempt = activeMeta?.attempts?.find((a) => !a.ok);

  const up = display.change >= 0;
  const Arrow = up ? ArrowUpRight : ArrowDownRight;
  const list = results ?? [];

  const chartData = tab === "intraday" ? intraday : kline;
  const currentChartError = chartData.length ? "" : chartError || `${tab === "intraday" ? "分时" : "日K"}未返回真实数据。`;

  return (
    <div className="h-full min-h-0 flex flex-col lg:flex-row gap-2.5 p-2.5 overflow-hidden">
      <div className="hidden lg:flex flex-col gap-4 min-h-0 overflow-y-auto pr-1 shrink-0" style={{ width: leftWidth }}>
        <WatchlistPanel visible={visible} compact currentSymbol={code} onSelect={onSelect} />

        <div className="rounded-xl border border-border bg-card p-3">
          <p className="text-[10px] tracking-[0.18em] text-muted-foreground uppercase mb-2 px-1">主要指数</p>
          {indices && indices.length > 0 ? (
            <div className="space-y-1">
              {indices.map((idx) => {
                const chg = idx.changePct;
                return (
                  <div key={idx.code} className="flex items-center justify-between px-1.5 py-1 text-xs rounded hover:bg-secondary/40">
                    <span className="truncate text-foreground/90">{idx.name}</span>
                    <span className={"font-mono tabular " + trendClass(chg ?? 0)}>
                      {idx.price != null ? fmt(idx.price, 2) : "—"}
                      {chg != null ? ` ${signed(chg)}%` : ""}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rounded-lg bg-secondary/40 px-3 py-3 text-xs text-muted-foreground leading-relaxed">
              正在同步宽基指数实时快照…
            </div>
          )}
        </div>

        <div className="rounded-xl border border-border bg-card flex flex-col overflow-hidden">
          <div className="p-2.5 border-b border-border">
            <div className="flex items-center gap-2 rounded-lg bg-input px-2.5 py-1.5">
              <Search className="size-3.5 text-muted-foreground" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索全 A 股（名称/代码/首字母）" className="bg-transparent outline-none text-xs w-full placeholder:text-muted-foreground" />
              {searching && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
            </div>
            <p className="text-[9px] text-muted-foreground/70 mt-1.5 px-0.5">{searchError ? "搜索服务异常" : results ? `全市场结果 ${list.length} 条` : "输入代码或简称"}</p>
          </div>
          <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
            {list.length === 0 && searchError && <div className="text-center text-[11px] text-destructive/80 py-8">搜索服务暂时不可用，请稍后重试</div>}
            {list.length === 0 && !searchError && Array.isArray(results) && <div className="text-center text-[11px] text-muted-foreground py-8">未找到匹配股票</div>}
            {list.map((s) => (
              <button key={s.code} onClick={() => onSelect(s.code)} className={`w-full flex items-center justify-between px-2 py-2 rounded-lg transition-colors ${s.code === display.code ? "bg-sidebar-accent" : "hover:bg-sidebar-accent/50"}`}>
                <div className="text-left leading-tight min-w-0">
                  <div className="text-sm text-foreground truncate">{s.name}</div>
                  <div className="text-[10px] text-muted-foreground tabular">{s.code}{s.exchange ? ` · ${s.exchange}` : ""}</div>
                </div>
                <div className="text-right leading-tight">
                  <div className={`text-xs tabular ${trendClass(s.changePct ?? 0)}`}>{s.price ? fmt(s.price) : "—"}</div>
                  <div className={`text-[10px] tabular ${trendClass(s.changePct ?? 0)}`}>{typeof s.changePct === "number" && Number.isFinite(s.changePct) ? `${signed(s.changePct)}%` : "—"}</div>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>

      <div
        onPointerDown={startLeftResize}
        className="hidden lg:flex w-2 -mx-2 cursor-col-resize items-stretch justify-center rounded-full group"
        title="拖动调整左侧栏宽度"
      >
        <div className="my-2 w-1 rounded-full bg-border transition-colors group-hover:bg-gold/60" />
      </div>

      <div className="flex flex-col gap-4 min-h-0 flex-1 min-w-0 overflow-y-auto">
        {/* 窄窗口搜索入口（lg 以下左侧栏隐藏时仍可搜股切换） */}
        <div className="lg:hidden rounded-xl border border-border bg-card p-2.5 relative">
          <div className="flex items-center gap-2 rounded-lg bg-input px-2.5 py-1.5">
            <Search className="size-3.5 text-muted-foreground" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索全 A 股（名称/代码/拼音首字母）"
              className="bg-transparent outline-none text-xs w-full placeholder:text-muted-foreground"
            />
            {searching && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
          </div>
          {q.trim() && (
            <div className="mt-1.5 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover p-1 space-y-0.5">
              {searchError ? (
                <div className="text-center text-[11px] text-destructive py-3">搜索服务暂时不可用</div>
              ) : list.length === 0 ? (
                <div className="text-center text-[11px] text-muted-foreground py-3">未找到匹配股票</div>
              ) : (
                list.map((s) => (
                  <button
                    key={s.code}
                    onClick={() => { onSelect(s.code); setQ(""); }}
                    className="w-full flex items-center justify-between px-2.5 py-1.5 rounded-md text-xs hover:bg-secondary"
                  >
                    <span className="truncate">{s.name} <span className="text-muted-foreground tabular">{s.code}</span></span>
                    <span className={`tabular ${trendClass(s.changePct ?? 0)}`}>{s.price ? fmt(s.price) : ""}</span>
                  </button>
                ))
              )}
            </div>
          )}
        </div>
        {(history.recent.length > 0 || history.frequent.length > 0) && (
          <div className="rounded-xl border border-border bg-card px-3 py-2 flex items-center gap-2 min-w-0">
            <div className="flex rounded-md bg-secondary/60 p-0.5 text-[11px] shrink-0">
              <button onClick={() => setHistoryTab("recent")} className={`flex items-center gap-1 rounded px-2 py-0.5 ${historyTab === "recent" ? "bg-card text-foreground" : "text-muted-foreground"}`}>
                <History className="size-3" /> 最近
              </button>
              <button onClick={() => setHistoryTab("frequent")} className={`flex items-center gap-1 rounded px-2 py-0.5 ${historyTab === "frequent" ? "bg-card text-foreground" : "text-muted-foreground"}`}>
                <Star className="size-3" /> 常看
              </button>
            </div>
            <div className="flex-1 min-w-0 flex items-center gap-1.5 overflow-x-auto">
              {(historyTab === "recent" ? history.recent : history.frequent).map((h) => (
                <button
                  key={h.code}
                  onClick={() => onSelect(h.code)}
                  title={`${h.code} · 查看 ${h.count} 次 · 最近 ${h.last_viewed}`}
                  className={`shrink-0 text-[11px] px-2 py-1 rounded-md transition-colors ${h.code === code ? "bg-accent text-gold" : "bg-secondary/50 text-foreground/80 hover:bg-secondary"}`}
                >
                  {h.name}{historyTab === "frequent" && <span className="ml-1 text-muted-foreground">×{h.count}</span>}
                </button>
              ))}
            </div>
            <button
              onClick={async () => { if (await confirm("清除全部行情搜索记录？")) void api.clearMarketHistory().then(loadHistory).catch(() => {}); }}
              className="grid place-items-center size-6 rounded text-muted-foreground hover:text-destructive shrink-0"
              title="清除搜索记录"
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        )}
        <motion.div layout className="rounded-xl border border-border bg-card p-4">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="flex items-center gap-2.5">
                <h1 className="font-display" style={{ fontSize: 22, fontWeight: 600 }}>{display.name || meta.name}</h1>
                <span className="text-xs text-muted-foreground tabular px-1.5 py-0.5 rounded bg-secondary">{display.code}</span>
                {display.industry && <span className="text-xs text-muted-foreground px-1.5 py-0.5 rounded bg-secondary">{display.industry}</span>}
              </div>
              {quoteLoading ? (
                <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" /> 正在获取真实行情
                </div>
              ) : quoteError ? (
                <div className="mt-3 text-sm text-destructive">行情获取失败：{quoteError}</div>
              ) : (
                <div className="mt-2 flex items-end gap-3">
                  <AnimatedNumber value={display.price} className={`${trendClass(display.change)}`} decimals={display.price > 0 && display.price < 5 ? 3 : 2} />
                  <div className={`flex items-center gap-1 ${trendClass(display.change)}`}>
                    <Arrow className="size-4" />
                    <span className="tabular text-sm">{signed(display.change, display.price > 0 && display.price < 5 ? 3 : 2)}</span>
                    <span className="tabular text-sm">({signed(display.changePct)}%)</span>
                  </div>
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setRefreshTick((n) => n + 1)}
                disabled={quoteLoading || silentRefreshing}
                title="刷新实时行情"
                className="flex items-center gap-1.5 rounded-lg bg-secondary hover:bg-secondary/70 px-3 py-2 text-xs text-foreground/85 disabled:opacity-50 transition-colors"
              >
                <RefreshCw className={`size-3.5 ${silentRefreshing ? "animate-spin" : ""}`} /> 刷新
              </button>
              <button onClick={() => onResearch(display.code, display.name || meta.name)} disabled={quoteLoading} className="flex items-center gap-1.5 rounded-lg bg-gold px-3.5 py-2 text-sm text-gold-foreground hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all">
                <TrendingUp className="size-4" strokeWidth={2.4} /> 深度研究
              </button>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-4 sm:grid-cols-6 gap-x-4 gap-y-2 text-xs">
            {(() => {
              const pDec = display.price > 0 && display.price < 5 ? 3 : 2;
              return [["今开", display.open ? fmt(display.open, pDec) : "—"], ["最高", display.high ? fmt(display.high, pDec) : "—"], ["最低", display.low ? fmt(display.low, pDec) : "—"], ["昨收", display.prevClose ? fmt(display.prevClose, pDec) : "—"], ["成交量", display.volume], ["成交额", display.turnover]];
            })().map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <span className="text-muted-foreground text-[10px]">{k}</span>
                <span className="tabular text-foreground/90">{v}</span>
              </div>
            ))}
          </div>
        </motion.div>

        <div className="rounded-xl border border-border bg-card flex-1 min-h-0 flex flex-col overflow-hidden">
          <div className="flex flex-wrap items-center gap-1 p-2 border-b border-border">
            {([[1, "分时"], [5, "5日"]] as const).map(([value, label]) => (
              <button key={value} aria-pressed={tab === "intraday" && days === value}
                className="rounded px-2 py-1 aria-pressed:bg-secondary" onClick={() => { setDays(value); setTab("intraday"); }}>{label}</button>
            ))}
            {([["daily", "日K"], ["weekly", "周K"], ["monthly", "月K"]] as const).map(([value, label]) => (
              <button key={value} aria-pressed={tab === "kline" && period === value}
                className="rounded px-2 py-1 aria-pressed:bg-secondary" onClick={() => { setPeriod(value); setTab("kline"); }}>{label}</button>
            ))}
            {tab === "kline" && <select aria-label="复权方式" className="rounded border bg-input px-1 py-1" value={adjust} onChange={(event) => setAdjust(event.target.value)}>
              <option value="qfq">前复权</option><option value="hfq">后复权</option><option value="">不复权</option>
            </select>}
            {tab === "kline" && actualAdjust !== undefined && actualAdjust !== adjust && <span className="text-xs text-muted-foreground">数据源返回不复权</span>}
            {tab === "intraday" && days === 5 && !!tradingDays && tradingDays < 5 && <span className="text-xs text-muted-foreground">已返回 {tradingDays} 个交易日</span>}
            <span className="ml-auto text-[10px] text-muted-foreground tabular px-2 text-right leading-relaxed">
              {dataAsOf || activeMeta?.trading_date || chartSourceLabel ? (
                <>
                  {activeMeta?.trading_date && <span className="text-foreground/70">{activeMeta.trading_date} · </span>}
                  <span>{dataAsOf ? `更新 ${dataAsOf}` : "等待真实数据"}</span>
                  {chartSourceLabel && <span className="ml-1 px-1.5 py-0.5 rounded bg-secondary text-foreground/80">{chartSourceLabel}</span>}
                  {typeof activeMeta?.elapsed_ms === "number" && <span className="ml-1 text-muted-foreground/60">{activeMeta.elapsed_ms}ms</span>}
                </>
              ) : (
                "等待真实数据"
              )}
            </span>
          </div>
          <div className="flex-1 min-h-0 p-2">
            <AnimatePresence mode="wait">
              <motion.div key={tab + chartKey} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }} className="h-full w-full">
                {quoteLoading || (silentRefreshing && !chartData.length) ? (
                  <div className="flex h-full flex-col gap-2 p-3"><Skeleton className="flex-1" /><Skeleton className="h-[28%]" /></div>
                ) : chartData.length ? (
                  tab === "intraday" ? <IntradayChart data={intraday} prevClose={days === 1 ? display.prevClose : 0} code={display.code} /> : <KLineChart data={kline} code={display.code} />
                ) : (
                  <DataUnavailable text={quoteError || failedAttempt?.error || currentChartError} />
                )}
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-4 min-h-0 overflow-y-auto lg:w-[220px] lg:shrink-0">
        <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">行情源诊断</summary>
          <DiagnosticsCard diagnostics={diagnostics} error={diagnosticsError} onRefresh={refreshDiagnostics} refreshing={diagRefreshing} />
        </details>
        {book.asks.length || book.bids.length ? (
          <OrderBook asks={book.asks} bids={book.bids} prevClose={display.prevClose} />
        ) : (
          <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">五档盘口未返回真实数据。</div>
        )}

        <div className="rounded-xl border border-border bg-card overflow-hidden">
          <div className="px-3 py-2.5 border-b border-border text-sm">技术指标</div>
          <div className="p-1.5 space-y-0.5">
            {indicators.length ? indicators.map((ind, i) => (
              <motion.div key={ind.name} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.05 * i }} className="flex items-center justify-between px-2 py-1.5 text-xs rounded-lg hover:bg-secondary/50">
                <span className="text-muted-foreground">{ind.name}</span>
                <div className="flex items-center gap-2">
                  <span className="tabular text-foreground/90">{ind.value}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ color: signalColor[ind.signal] ?? "var(--muted-foreground)", background: "var(--secondary)" }}>{ind.signal}</span>
                </div>
              </motion.div>
            )) : <div className="px-2 py-6 text-xs text-muted-foreground text-center">技术指标未返回真实数据。</div>}
          </div>
        </div>

        <div className="rounded-xl border border-border bg-card overflow-hidden">
          <div className="px-3 py-2.5 border-b border-border text-sm">基本面</div>
          <div className="grid grid-cols-2 gap-px bg-border">
            {[
              ["市值", display.marketCap],
              ["市盈率 PE", display.pe < 0 ? `${fmt(display.pe, 1)}（亏）` : display.pe > 0 ? fmt(display.pe, 1) : "—"],
              ["市净率 PB", display.pb ? fmt(display.pb, 1) : "—"],
              ["ROE", display.roe ? fmt(display.roe, 1) + "%" : "—"],
            ].map(([k, v]) => (
              <div key={k} className="bg-card p-3">
                <div className="text-[10px] text-muted-foreground">{k}</div>
                <div className="tabular text-sm text-foreground/90 mt-0.5">{v}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
