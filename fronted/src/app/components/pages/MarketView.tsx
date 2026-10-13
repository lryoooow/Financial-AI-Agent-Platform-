import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { AlertTriangle, ArrowDownRight, ArrowRight, ArrowUpRight, Loader2, RefreshCw, Search } from "lucide-react";
import { api, type Book, type ChartBundle, type IndexSummary, type Indicator, type ResolveHit } from "../../api/client";
import { getFrequent, getRecent, trackUsage } from "../../lib/usage";
import { WATCHLIST } from "../../data/market";
import { fmt, signed, trendClass } from "../../lib/format";
import { AnimatedNumber } from "../AnimatedNumber";
import { OrderBook } from "../OrderBook";
import { IntradayChart } from "../charts/IntradayChart";
import { KLineChart, type TechnicalIndicator } from "../charts/KLineChart";
import { MarketChatPanel } from "./MarketChatPanel";

const EMPTY_BOOK: Book = { asks: [], bids: [] };
/** 大盘指数代码集（与后端 entity.INDEX_CATALOG 对应）。 */
const INDEX_SET = new Set(["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]);
const isIndexCode = (c: string) => INDEX_SET.has(c);
// 日 K 区间：短区间同样可选，均线条数随区间长度递增（5日→MA5，10日→MA5/10，20日→MA5/10/20…）
const RANGE_OPTIONS = [5, 10, 20, 60, 120, 250] as const;
const signalColor: Record<string, string> = {
  多头: "var(--up)",
  金叉: "var(--up)",
  超买: "var(--up)",
  死叉: "var(--down)",
  空头: "var(--down)",
  超卖: "var(--down)",
  中性: "var(--muted-foreground)",
};

interface ActiveModel { provider: string; model: string; accent: string; }

function errorOf(value?: { provenance?: { error?: string | null } } | null): string {
  return value?.provenance?.error || "";
}

function Unavailable({ text, onRetry }: { text: string; onRetry: () => void }) {
  return (
    <div className="grid h-full min-h-[360px] place-items-center px-6 text-center">
      <div className="max-w-sm">
        <AlertTriangle className="mx-auto size-7 text-destructive" />
        <p className="mt-3 text-sm text-foreground">真实行情暂不可用</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{text}</p>
        <button onClick={onRetry} className="mx-auto mt-3 flex items-center gap-1.5 rounded-md bg-secondary px-3 py-1.5 text-xs hover:bg-secondary/70">
          <RefreshCw className="size-3.5" /> 重试
        </button>
      </div>
    </div>
  );
}

export function MarketView({
  code,
  active,
  activeModel,
  onSelect,
  onOpenResearch,
  onGoSettings,
}: {
  code: string;
  active: boolean;
  activeModel: ActiveModel | null;
  onSelect: (code: string) => void;
  onOpenResearch: (code: string) => void;
  onGoSettings: () => void;
}) {
  const [bundleState, setBundleState] = useState<{
    code: string;
    range: (typeof RANGE_OPTIONS)[number];
    data: ChartBundle;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [tab, setTab] = useState<"intraday" | "kline">("intraday");
  const [range, setRange] = useState<(typeof RANGE_OPTIONS)[number]>(120);
  const [indicator, setIndicator] = useState<TechnicalIndicator>("MACD");
  const [visibleRange, setVisibleRange] = useState<{ start: string; end: string }>({ start: "", end: "" });
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ResolveHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [indexes, setIndexes] = useState<IndexSummary[]>([]);
  const [usageTick, setUsageTick] = useState(0); // 选中标的后刷新侧栏常用/最近区
  const searchBoxRef = useRef<HTMLDivElement>(null);
  // 点击搜索框外部 / Esc 关闭联想下拉（旧版会一直悬停不消失）
  useEffect(() => {
    if (!dropdownOpen) return;
    const onPointerDown = (e: PointerEvent) => {
      if (searchBoxRef.current && !searchBoxRef.current.contains(e.target as Node)) setDropdownOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDropdownOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [dropdownOpen]);
  const [refreshTick, setRefreshTick] = useState(0);
  const requestGeneration = useRef(0);
  const bundle = bundleState?.code === code && bundleState.range === range ? bundleState.data : null;

  useEffect(() => {
    let cancelled = false;
    const pull = () => api.marketIndexes()
      .then((r) => { if (!cancelled) setIndexes(r.indexes || []); })
      .catch(() => {});
    void pull();
    const timer = window.setInterval(pull, 30000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    if (!active) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    const requestedCode = code;
    const requestedRange = range;
    setBundleState((current) => current?.code === requestedCode && current.range === requestedRange ? current : null);
    setLoading(true);
    setLoadError("");
    api.chartBundle(requestedCode, requestedRange, "qfq", controller.signal)
      .then((next) => {
        if (generation === requestGeneration.current && !controller.signal.aborted) {
          setBundleState({ code: requestedCode, range: requestedRange, data: next });
        }
      })
      .catch((error) => {
        if (generation === requestGeneration.current && (error as DOMException)?.name !== "AbortError") {
          setLoadError(error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        if (generation === requestGeneration.current && !controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [active, code, range, refreshTick]);

  useEffect(() => {
    if (!active || tab !== "intraday") return;
    let stopped = false;
    let controller: AbortController | null = null;
    const timer = window.setInterval(async () => {
      controller?.abort();
      const tickController = new AbortController();
      controller = tickController;
      const requestedCode = code;
      const [quoteResult, intradayResult] = await Promise.allSettled([
        api.quote(requestedCode, tickController.signal),
        api.intraday(requestedCode, tickController.signal),
      ]);
      if (stopped || tickController.signal.aborted || controller !== tickController) return;
      setBundleState((current) => {
        if (!current || current.code !== requestedCode) return current;
        return {
          ...current,
          data: {
            ...current.data,
            quote: quoteResult.status === "fulfilled" ? quoteResult.value : current.data.quote,
            intraday: intradayResult.status === "fulfilled" ? intradayResult.value : current.data.intraday,
          },
        };
      });
    }, 30_000);
    return () => {
      stopped = true;
      controller?.abort();
      window.clearInterval(timer);
    };
  }, [active, code, tab]);

  useEffect(() => {
    const keyword = query.trim();
    if (!keyword) {
      setResults([]);
      setDropdownOpen(false);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      api.resolve(keyword, 8, controller.signal)
        .then((r) => { setResults(r); setDropdownOpen(r.length > 0); })
        .catch((error) => {
          if ((error as DOMException)?.name !== "AbortError") setResults([]);
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 220);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [query]);

  const selectCode = (nextCode: string, name?: string) => {
    onSelect(nextCode);
    if (name) trackUsage(nextCode, name);
    setUsageTick((v) => v + 1);
    setQuery("");
    setResults([]);
    setDropdownOpen(false);
  };
  const setChartVisibleRange = useCallback((start: string, end: string) => {
    setVisibleRange((current) => current.start === start && current.end === end ? current : { start, end });
  }, []);

  const quote = bundle?.quote;
  const intraday = bundle?.intraday.data || [];
  const kline = bundle?.kline.data || [];
  const book = bundle?.book || EMPTY_BOOK;
  const indicators = bundle?.indicators.data || [];
  const series = bundle?.indicators.series || [];
  const quoteError = errorOf(quote);
  const intradayError = errorOf(bundle?.intraday);
  const klineError = errorOf(bundle?.kline);
  const price = quote?.price || kline[kline.length - 1]?.close || intraday[intraday.length - 1]?.price || 0;
  const prevClose = quote?.prevClose || kline[Math.max(0, kline.length - 2)]?.close || intraday[0]?.price || price;
  const change = quote?.change ?? (price - prevClose);
  const changePct = quote?.changePct ?? (prevClose ? ((price - prevClose) / prevClose) * 100 : 0);
  const meta = useMemo(() => WATCHLIST.find((item) => item.code === code), [code]);
  const name = quote?.name || meta?.name || code;
  const Arrow = change >= 0 ? ArrowUpRight : ArrowDownRight;
  const activeResult = tab === "intraday" ? bundle?.intraday : bundle?.kline;
  const activeError = tab === "intraday" ? intradayError : klineError;

  const isIdx = isIndexCode(code);
  void usageTick; // 选中标的时自增，驱动下方 frequent/recent 重新读取
  const frequent = getFrequent(6);
  const recent = getRecent(5);

  return (
    <div className="flex h-full flex-col overflow-y-auto p-3 lg:overflow-hidden lg:p-4">
      {/* 大盘条：五指数实时（30s 轮询），点击进入指数走势视图 */}
      <div className="mb-3 grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
        {indexes.map((ix) => (
          <button
            key={ix.code}
            onClick={() => selectCode(ix.code, ix.name)}
            className={`flex items-center justify-between rounded-xl border px-3 py-2.5 text-left transition-colors ${code === ix.code ? "border-gold/50 bg-accent" : "border-border bg-card hover:border-border/80"}`}
          >
            <div className="min-w-0">
              <div className="text-[11px] text-muted-foreground">{ix.name}</div>
              <div className={`mt-0.5 font-display tabular text-base ${trendClass(ix.changePct)}`}>{fmt(ix.price, 2)}</div>
            </div>
            <div className={`shrink-0 text-right text-xs tabular ${trendClass(ix.changePct)}`}>
              <div>{signed(ix.changePct)}%</div>
              {ix.turnover && <div className="mt-0.5 text-[9px] text-muted-foreground">{ix.turnover}</div>}
            </div>
          </button>
        ))}
      </div>
      <div className="grid min-h-0 flex-1 gap-3 lg:h-full lg:grid-cols-[220px_minmax(0,1fr)_330px]">
        <aside className="rounded-xl border border-border bg-card p-3 lg:min-h-0 lg:overflow-y-auto">
          <div className="relative" ref={searchBoxRef}>
            <div className="flex items-center gap-2 rounded-lg border border-transparent bg-input px-3 py-2 focus-within:border-gold/40">
              <Search className="size-4 text-muted-foreground" />
              <input value={query} onChange={(event) => setQuery(event.target.value)} onFocus={() => { if (results.length) setDropdownOpen(true); }} placeholder="代码或股票名称" className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground" />
              {searching && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
            </div>
            <AnimatePresence>
              {dropdownOpen && results.length > 0 && (
                <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="absolute left-0 right-0 top-[44px] z-30 overflow-hidden rounded-lg border border-border bg-popover shadow-xl">
                  {results.map((item) => (
                    <button key={item.code} onClick={() => selectCode(item.code, item.name)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-secondary">
                      <span className="truncate text-xs">{item.name}</span>
                      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{item.code}</span>
                    </button>
                  ))}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
          {frequent.length > 0 && (
            <>
              <p className="mb-2 mt-5 px-1 text-[10px] uppercase tracking-[0.16em] text-muted-foreground">常用 · 按你的使用学习</p>
              <div className="space-y-1">
                {frequent.map((item) => (
                  <button key={item.code} onClick={() => selectCode(item.code, item.name)} className={`flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left transition-colors ${code === item.code ? "bg-accent text-gold" : "hover:bg-secondary"}`}>
                    <div className="min-w-0">
                      <div className="truncate text-xs">{item.name}</div>
                      <div className="font-mono text-[9px] text-muted-foreground">{item.code}</div>
                    </div>
                    {code === item.code && <span className="size-1.5 rounded-full bg-gold" />}
                  </button>
                ))}
              </div>
            </>
          )}
          {recent.length > 0 && (
            <>
              <p className="mb-2 mt-4 px-1 text-[10px] uppercase tracking-[0.16em] text-muted-foreground">最近使用</p>
              <div className="flex flex-wrap gap-1">
                {recent.map((item) => (
                  <button key={item.code} onClick={() => selectCode(item.code, item.name)} className="rounded-md bg-secondary/60 px-1.5 py-1 text-[10px] text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
                    {item.name}
                  </button>
                ))}
              </div>
            </>
          )}
          <p className="mb-2 mt-5 px-1 text-[10px] uppercase tracking-[0.16em] text-muted-foreground">快捷标的</p>
          <div className="space-y-1">
            {WATCHLIST.map((item) => (
              <button key={item.code} onClick={() => selectCode(item.code, item.name)} className={`flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left transition-colors ${code === item.code ? "bg-accent text-gold" : "hover:bg-secondary"}`}>
                <div className="min-w-0">
                  <div className="truncate text-xs">{item.name}</div>
                  <div className="font-mono text-[9px] text-muted-foreground">{item.code}</div>
                </div>
                {code === item.code && <span className="size-1.5 rounded-full bg-gold" />}
              </button>
            ))}
          </div>
          <div className="mt-5 rounded-lg bg-secondary/40 p-3 text-[10.5px] leading-relaxed text-muted-foreground">
            可搜索全部 A 股；报价、分时、K 线与盘口分别显示真实失败状态，任一来源失败不再阻断其他图表。
          </div>
        </aside>

        <section className="flex min-h-[720px] min-w-0 flex-col gap-3 lg:min-h-0">
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="font-display text-xl font-semibold">{name}</h1>
                  <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{code}</span>
                  {quote?.industry && <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">{quote.industry}</span>}
                  {quote?.stale && <span className="rounded bg-up/10 px-1.5 py-0.5 text-[10px] text-up">延迟数据</span>}
                </div>
                {loading && !bundle ? (
                  <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 正在加载真实行情</div>
                ) : price > 0 ? (
                  <div className="mt-2 flex items-end gap-3">
                    <AnimatedNumber value={price} decimals={2} className={trendClass(change)} />
                    <span className={`flex items-center gap-1 text-sm tabular ${trendClass(change)}`}><Arrow className="size-4" />{signed(change)} ({signed(changePct)}%)</span>
                  </div>
                ) : (
                  <p className="mt-2 text-xs text-destructive">{quoteError || "报价未返回；仍可查看历史图表"}</p>
                )}
              </div>
              <div className="flex gap-2">
                <button onClick={() => setRefreshTick((value) => value + 1)} disabled={loading} className="flex items-center gap-1.5 rounded-lg bg-secondary px-3 py-2 text-xs hover:bg-secondary/70 disabled:opacity-50">
                  <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} /> 刷新
                </button>
                {!isIdx && (
                  <button onClick={() => onOpenResearch(code)} className="flex items-center gap-1.5 rounded-lg bg-gold px-3 py-2 text-xs text-[#17140a] hover:brightness-110">
                    前往研究任务 <ArrowRight className="size-3.5" />
                  </button>
                )}
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-xs sm:grid-cols-6">
              {[["今开", quote?.open], ["最高", quote?.high], ["最低", quote?.low], ["昨收", prevClose], ["成交量", quote?.volume], ["成交额", quote?.turnover]].map(([label, value]) => (
                <div key={String(label)}><div className="text-[10px] text-muted-foreground">{label}</div><div className="mt-0.5 tabular">{typeof value === "number" ? (value ? fmt(value, 2) : "—") : (value || "—")}</div></div>
              ))}
            </div>
          </div>

          <div className="flex min-h-[580px] flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex flex-wrap items-center gap-1 border-b border-border p-2">
              {(["intraday", "kline"] as const).map((item) => (
                <button key={item} onClick={() => setTab(item)} className={`rounded-lg px-3 py-1.5 text-xs ${tab === item ? "bg-secondary text-foreground" : "text-muted-foreground"}`}>{item === "intraday" ? "分时" : "日 K 线"}</button>
              ))}
              {tab === "kline" && <div className="ml-2 flex gap-1">{RANGE_OPTIONS.map((item) => <button key={item} onClick={() => setRange(item)} className={`rounded px-1.5 py-1 text-[10px] ${range === item ? "bg-gold text-[#17140a]" : "bg-secondary text-muted-foreground"}`}>{item}日</button>)}</div>}
              <div className="ml-auto text-right text-[10px] text-muted-foreground">
                <span>{activeResult?.trading_date || "等待数据"}</span>
                {activeResult?.interval && <span className="ml-1 rounded bg-secondary px-1.5 py-0.5">{activeResult.interval}</span>}
                {activeResult?.provenance?.source && <span className="ml-1 rounded bg-secondary px-1.5 py-0.5">{activeResult.provenance.source}</span>}
              </div>
            </div>
            <div className="min-h-0 flex-1 p-2">
              {loading && !bundle ? (
                <div className="grid h-full place-items-center"><Loader2 className="size-6 animate-spin text-gold" /></div>
              ) : tab === "intraday" && intraday.length ? (
                <IntradayChart data={intraday} prevClose={prevClose} code={code} />
              ) : tab === "kline" && kline.length ? (
                <KLineChart data={kline} indicators={series} code={code} indicator={indicator} onIndicatorChange={setIndicator} onVisibleRangeChange={setChartVisibleRange} />
              ) : (
                <Unavailable text={loadError || activeError || "该标的暂未返回有效数据"} onRetry={() => setRefreshTick((value) => value + 1)} />
              )}
            </div>
          </div>
        </section>

        <aside className="flex min-h-0 flex-col gap-3 lg:overflow-y-auto lg:pr-1">
          {book.asks.length || book.bids.length ? <OrderBook asks={book.asks} bids={book.bids} prevClose={prevClose} /> : <div className="rounded-xl border border-border bg-card p-3 text-xs text-muted-foreground">{errorOf(bundle?.book) || "五档盘口暂未返回"}</div>}
          <IndicatorCard indicators={indicators} />
          {!isIdx && (
          <div className="rounded-xl border border-border bg-card p-3">
            <div className="mb-2 text-sm">基本面快照</div>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {[["市值", quote?.marketCap], ["市盈率 PE", quote?.pe], ["市净率 PB", quote?.pb], ["ROE", quote?.roe ? `${fmt(quote.roe, 1)}%` : "—"]].map(([label, value]) => <div key={String(label)} className="rounded-lg bg-secondary/40 p-2"><div className="text-[10px] text-muted-foreground">{label}</div><div className="mt-0.5 tabular">{typeof value === "number" ? (value ? fmt(value, 1) : "—") : (value || "—")}</div></div>)}
            </div>
          </div>
          )}
          <MarketChatPanel
            code={code}
            kind={tab}
            indicator={indicator}
            visibleRange={visibleRange}
            activeModel={activeModel}
            onGoSettings={onGoSettings}
          />
        </aside>
      </div>
    </div>
  );
}

function IndicatorCard({ indicators }: { indicators: Indicator[] }) {
  return (
    <div className="rounded-xl border border-border bg-card">
      <div className="border-b border-border px-3 py-2.5 text-sm">技术指标摘要</div>
      <div className="max-h-60 space-y-0.5 overflow-y-auto p-1.5">
        {indicators.length ? indicators.map((item) => (
          <div key={item.name} className="flex items-center justify-between rounded-lg px-2 py-1.5 text-xs hover:bg-secondary/50">
            <span className="text-muted-foreground">{item.name}</span>
            <div className="flex items-center gap-2"><span className="tabular">{item.value}</span><span className="rounded bg-secondary px-1.5 py-0.5 text-[10px]" style={{ color: signalColor[item.signal] || "var(--muted-foreground)" }}>{item.signal}</span></div>
          </div>
        )) : <div className="py-6 text-center text-xs text-muted-foreground">指标数据不足</div>}
      </div>
    </div>
  );
}
