import { useEffect, useRef, useState, type PointerEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowUpRight, ArrowDownRight, TrendingUp, Search, Loader2, AlertTriangle, Database, Globe2, ShieldCheck, RefreshCw } from "lucide-react";
import { WATCHLIST, type Stock } from "../../data/market";
import { api, type Book, type Indicator, type IntradayPoint, type MarketDiagnostics, type Quote, type ResolveHit } from "../../api/client";
import { AnimatedNumber } from "../AnimatedNumber";
import { OrderBook } from "../OrderBook";
import { IntradayChart } from "../charts/IntradayChart";
import { KLineChart } from "../charts/KLineChart";
import { fmt, signed, trendClass } from "../../lib/format";

const EMPTY_BOOK: Book = { asks: [], bids: [] };
const LEFT_WIDTH_KEY = "finresearch_market_left_width";

const signalColor: Record<string, string> = {
  多头: "var(--up)",
  金叉: "var(--up)",
  超买: "var(--up)",
  死叉: "var(--down)",
  空头: "var(--down)",
  中性: "var(--muted-foreground)",
};

function fallbackMeta(code: string): Pick<Stock, "code" | "name" | "exchange" | "industry"> {
  const s = WATCHLIST.find((x) => x.code === code);
  return {
    code,
    name: s?.name || code,
    exchange: s?.exchange || "A股",
    industry: s?.industry || "",
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
}: {
  code: string;
  onSelect: (c: string) => void;
  onResearch: (c: string) => void;
}) {
  const meta = fallbackMeta(code);
  const [stock, setStock] = useState<Quote | null>(null);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const [tab, setTab] = useState<"intraday" | "kline">("intraday");
  const [q, setQ] = useState("");

  const [intraday, setIntraday] = useState<IntradayPoint[]>([]);
  const [kline, setKline] = useState<any[]>([]);
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
  const [searching, setSearching] = useState(false);
  const [favPrices, setFavPrices] = useState<Record<string, { price: number; changePct: number }>>({});
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
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  };

  useEffect(() => {
    const seq = ++loadSeq.current;
    const ac = new AbortController();
    let cancelled = false;
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

    const timer = window.setTimeout(() => {
    (async () => {
      try {
        const qq = await api.quote(code, ac.signal);
        if (cancelled || ac.signal.aborted || seq !== loadSeq.current) return;
        if (!isUsableQuote(qq)) {
          const reason = qq?.provenance?.error || "行情接口未返回有效价格";
          setQuoteError(reason);
          return;
        }
        setStock(qq);
        setDataAsOf(qq.as_of || qq.provenance?.fetched_at || "");

        const [intraRes, klineRes, bookRes, indRes] = await Promise.allSettled([
          api.intraday(code, ac.signal),
          api.kline(code, 60, "qfq", ac.signal),
          api.book(code, ac.signal),
          api.indicators(code, ac.signal),
        ]);
        if (cancelled || ac.signal.aborted || seq !== loadSeq.current) return;

        if (intraRes.status === "fulfilled") {
          const v = intraRes.value;
          setIntraday(v.data || []);
          setIntradayMeta({ source: v.provenance?.source, attempts: v.provenance?.attempts, trading_date: v.trading_date, elapsed_ms: v.elapsed_ms });
        }
        if (klineRes.status === "fulfilled") {
          const v = klineRes.value;
          setKline(v.data || []);
          setKlineMeta({ source: v.provenance?.source, attempts: v.provenance?.attempts, trading_date: v.trading_date, elapsed_ms: v.elapsed_ms });
        }
        if (bookRes.status === "fulfilled") setBook(bookRes.value);
        if (indRes.status === "fulfilled" && indRes.value.length) setIndicators(indRes.value);

        const missing: string[] = [];
        if (intraRes.status !== "fulfilled" || !(intraRes.value.data || []).length) missing.push("分时");
        if (klineRes.status !== "fulfilled" || !(klineRes.value.data || []).length) missing.push("日K");
        if (missing.length) setChartError(`${missing.join("、")}未返回真实数据，请检查数据源或稍后重试。`);
      } catch (e) {
        if (!cancelled && !ac.signal.aborted && seq === loadSeq.current) {
          setQuoteError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled && !ac.signal.aborted && seq === loadSeq.current) setQuoteLoading(false);
      }
    })();
    }, 120);

    return () => {
      cancelled = true;
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [code]);

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
      setSearching(false);
      return;
    }
    setSearching(true);
    let cancelled = false;
    const ac = new AbortController();
    const t = setTimeout(async () => {
      try {
        const r = await api.resolve(query, 12, ac.signal);
        if (!cancelled) setResults(r);
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") return;
        if (!cancelled) setResults([]);
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

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const entries = await Promise.all(
        WATCHLIST.map(async (s) => {
          try {
            const qq = await api.quote(s.code);
            if (!isUsableQuote(qq)) return null;
            return [s.code, { price: qq.price, changePct: qq.changePct }] as const;
          } catch {
            return null;
          }
        })
      );
      if (cancelled) return;
      const m: Record<string, { price: number; changePct: number }> = {};
      for (const e of entries) if (e) m[e[0]] = e[1];
      setFavPrices(m);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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
  const filteredFav = WATCHLIST.filter((s) => s.name.includes(q) || s.code.includes(q.toUpperCase()));
  const list: { code: string; name: string; exchange?: string; price?: number; changePct?: number }[] =
    results ?? filteredFav.map((s) => ({ code: s.code, name: s.name, exchange: s.exchange, ...favPrices[s.code] }));

  const chartData = tab === "intraday" ? intraday : kline;
  const currentChartError = chartData.length ? "" : chartError || `${tab === "intraday" ? "分时" : "日K"}未返回真实数据。`;

  return (
    <div className="h-full min-h-0 flex flex-col lg:flex-row gap-4 p-5 overflow-hidden">
      <div className="hidden lg:flex flex-col gap-4 min-h-0 overflow-y-auto pr-1 shrink-0" style={{ width: leftWidth }}>
        <DiagnosticsCard diagnostics={diagnostics} error={diagnosticsError} onRefresh={refreshDiagnostics} refreshing={diagRefreshing} />

        <div className="rounded-xl border border-border bg-card p-3">
          <p className="text-[10px] tracking-[0.18em] text-muted-foreground uppercase mb-2 px-1">主要指数</p>
          <div className="rounded-lg bg-secondary/40 px-3 py-4 text-xs text-muted-foreground leading-relaxed">
            指数实时行情暂未接入真实数据源，本页不展示模拟指数。
          </div>
        </div>

        <div className="rounded-xl border border-border bg-card min-h-[260px] flex flex-col overflow-hidden">
          <div className="p-2.5 border-b border-border">
            <div className="flex items-center gap-2 rounded-lg bg-input px-2.5 py-1.5">
              <Search className="size-3.5 text-muted-foreground" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索全 A 股（名称/代码）" className="bg-transparent outline-none text-xs w-full placeholder:text-muted-foreground" />
              {searching && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
            </div>
            <p className="text-[9px] text-muted-foreground/70 mt-1.5 px-0.5">{results ? `全市场结果 ${list.length} 条` : "常用标的"}</p>
          </div>
          <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
            {list.length === 0 && results && <div className="text-center text-[11px] text-muted-foreground py-8">未找到匹配股票</div>}
            {list.map((s) => (
              <button key={s.code} onClick={() => onSelect(s.code)} className={`w-full flex items-center justify-between px-2 py-2 rounded-lg transition-colors ${s.code === display.code ? "bg-sidebar-accent" : "hover:bg-sidebar-accent/50"}`}>
                <div className="text-left leading-tight min-w-0">
                  <div className="text-sm text-foreground truncate">{s.name}</div>
                  <div className="text-[10px] text-muted-foreground tabular">{s.code}{s.exchange ? ` · ${s.exchange}` : ""}</div>
                </div>
                <div className="text-right leading-tight">
                  <div className={`text-xs tabular ${trendClass(s.changePct ?? 0)}`}>{s.price ? fmt(s.price) : "—"}</div>
                  <div className={`text-[10px] tabular ${trendClass(s.changePct ?? 0)}`}>{s.changePct == null ? "—" : `${signed(s.changePct)}%`}</div>
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

      <div className="flex flex-col gap-4 min-h-0 flex-1 min-w-0">
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
                  <AnimatedNumber value={display.price} className={`${trendClass(display.change)}`} decimals={2} />
                  <div className={`flex items-center gap-1 ${trendClass(display.change)}`}>
                    <Arrow className="size-4" />
                    <span className="tabular text-sm">{signed(display.change)}</span>
                    <span className="tabular text-sm">({signed(display.changePct)}%)</span>
                  </div>
                </div>
              )}
            </div>
            <button onClick={() => onResearch(display.code)} disabled={!!quoteError || quoteLoading} className="flex items-center gap-1.5 rounded-lg bg-gold px-3.5 py-2 text-sm text-[#17140a] hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all">
              <TrendingUp className="size-4" strokeWidth={2.4} /> 生成研究报告
            </button>
          </div>

          <div className="mt-3 grid grid-cols-4 sm:grid-cols-6 gap-x-4 gap-y-2 text-xs">
            {[["今开", display.open ? fmt(display.open) : "—"], ["最高", display.high ? fmt(display.high) : "—"], ["最低", display.low ? fmt(display.low) : "—"], ["昨收", display.prevClose ? fmt(display.prevClose) : "—"], ["成交量", display.volume], ["成交额", display.turnover]].map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <span className="text-muted-foreground text-[10px]">{k}</span>
                <span className="tabular text-foreground/90">{v}</span>
              </div>
            ))}
          </div>
        </motion.div>

        <div className="rounded-xl border border-border bg-card flex-1 min-h-0 flex flex-col overflow-hidden">
          <div className="flex items-center gap-1 p-2 border-b border-border">
            {([["intraday", "分时"], ["kline", "日K线"]] as const).map(([id, label]) => (
              <button key={id} onClick={() => setTab(id)} className="relative px-3.5 py-1.5 text-sm rounded-lg">
                {tab === id && <motion.div layoutId="chart-tab" className="absolute inset-0 rounded-lg bg-secondary" transition={{ type: "spring", stiffness: 400, damping: 30 }} />}
                <span className={`relative ${tab === id ? "text-foreground" : "text-muted-foreground"}`}>{label}</span>
              </button>
            ))}
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
              <motion.div key={tab + display.code} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }} className="h-full w-full">
                {quoteLoading ? (
                  <div className="h-full grid place-items-center text-sm text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>
                ) : chartData.length ? (
                  tab === "intraday" ? <IntradayChart data={intraday} prevClose={display.prevClose} code={display.code} /> : <KLineChart data={kline} code={display.code} />
                ) : (
                  <DataUnavailable text={quoteError || failedAttempt?.error || currentChartError} />
                )}
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-4 min-h-0 overflow-y-auto lg:w-[288px] lg:shrink-0">
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
            {[["市值", display.marketCap], ["市盈率 PE", display.pe ? fmt(display.pe, 1) : "—"], ["市净率 PB", display.pb ? fmt(display.pb, 1) : "—"], ["ROE", display.roe ? fmt(display.roe, 1) + "%" : "—"]].map(([k, v]) => (
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
