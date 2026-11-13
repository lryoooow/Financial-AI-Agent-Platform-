import { useEffect, useState } from "react";
import { ArrowDownRight, ArrowRight, ArrowUpRight, RefreshCw, Search } from "lucide-react";
import { api, type MarketOverview, type ReportListItem, type ResearchTaskSummary } from "../../api/client";
import { fmt, signed, trendClass } from "../../lib/format";
import { Skeleton } from "../ui/skeleton";
import { WatchlistPanel } from "../watchlist/WatchlistPanel";

function amount(value?: number | null) {
  return value == null || !Number.isFinite(value) ? "—" : fmt(value / 1e8, 1) + " 亿";
}

export function OverviewDashboard({ visible, market, onMarket, onSelect, onSearch, onRecap, onResearch, onReport }: {
  visible: boolean; market?: MarketOverview; onMarket: (data: MarketOverview) => void; onSelect: (code: string) => void;
  onSearch: () => void; onRecap: () => void; onResearch: () => void; onReport: (id: string) => void;
}) {
  const [tasks, setTasks] = useState<ResearchTaskSummary[]>();
  const [reports, setReports] = useState<ReportListItem[]>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [updates, setUpdates] = useState(0);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    const refresh = () => {
      api.researchTasks().then(({ tasks }) => { if (active) setTasks(tasks); }).catch(() => {});
      api.reportsList().then(({ reports }) => { if (active) setReports(reports); }).catch(() => {});
    };
    refresh();
    const timer = window.setInterval(refresh, 12_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [updates, visible]);

  async function refreshMarket() {
    setBusy(true);
    setError("");
    try { onMarket(await api.marketOverview(undefined, true)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  const breadth = market?.breadth;
  const total = breadth?.count ?? 0;
  const activeTasks = tasks?.filter((task) => task.status === "running" || task.status === "pending").slice(0, 6);
  const leader = market?.industries[0];
  return <div className="h-full overflow-y-auto p-3 sm:p-4">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h1 className="text-lg font-semibold">市场总览</h1>
      <button type="button" disabled={busy} onClick={() => void refreshMarket()} className="flex items-center gap-1 rounded border px-2 py-1 text-muted-foreground">
        <RefreshCw className={"size-3.5 " + (busy ? "animate-spin" : "")} /> 刷新市场
      </button>
    </div>
    {error && <p role="alert" className="mb-2 text-destructive">{error}</p>}
    {!market ? <Skeleton className="mb-3 h-36" /> : <>
      <div className="mb-3 grid gap-2 md:grid-cols-3">
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-1 text-muted-foreground">A 股涨跌广度</h2>
          <div className="flex flex-wrap items-center gap-2 font-mono tabular-nums">
            <span className="inline-flex items-center gap-1 text-up"><ArrowUpRight className="size-4" />{breadth?.up ?? "—"}</span>
            <span className="text-muted-foreground">平 {breadth?.flat ?? "—"}</span>
            <span className="inline-flex items-center gap-1 text-down"><ArrowDownRight className="size-4" />{breadth?.down ?? "—"}</span>
          </div>
          {total > 0 && <div className="my-1.5 flex h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
            <span className="bg-up" style={{ width: (breadth?.up ?? 0) / total * 100 + "%" }} />
            <span className="bg-down" style={{ width: (breadth?.down ?? 0) / total * 100 + "%", marginLeft: "auto" }} />
          </div>}
          <p className="text-xs text-muted-foreground">涨停 {breadth?.limit_up ?? "—"} · 跌停 {breadth?.limit_down ?? "—"}</p>
        </section>
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-1 text-muted-foreground">沪深市场成交额</h2>
          <p className="font-mono text-base tabular-nums">{amount(breadth?.turnover_yuan)}</p>
          <p className="text-xs text-muted-foreground">{breadth?.trading_date || "时点未获取"} · {breadth?.turnover_partial ? "盘中累计" : "较上交易日 " + amount(breadth?.turnover_change_yuan)}</p>
        </section>
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-1 text-muted-foreground">申万一级领涨</h2>
          <p className="text-base">{leader?.name ?? "—"} {leader && leader.changePct != null && <span className={"font-mono " + trendClass(leader.changePct)}>{signed(leader.changePct)}%</span>}</p>
          <p className="text-xs text-muted-foreground">东财同名板块主力净流入 {amount(leader?.net_flow_yuan)}</p>
        </section>
      </div>
      <section className="mb-3 rounded-lg border bg-card p-3">
        <h2 className="mb-2 font-medium">申万一级行业热力</h2>
        {!market.industries.length && <p className="text-muted-foreground">行业数据暂不可用。</p>}
        <div className="grid grid-cols-[repeat(auto-fit,minmax(105px,1fr))] gap-1.5">
          {market.industries.map((industry) => {
            const chg = industry.changePct;
            return <div key={industry.code} className={"flex justify-between gap-1 rounded px-2 py-1.5 text-xs " + ((chg ?? 0) > 0 ? "bg-[var(--up-soft)]" : (chg ?? 0) < 0 ? "bg-[var(--down-soft)]" : "bg-secondary")}
              title={"东财同名板块主力净流入 " + amount(industry.net_flow_yuan)}>
              <span className="truncate">{industry.name}</span><span className={"shrink-0 font-mono tabular-nums " + trendClass(chg ?? 0)}>{chg == null ? "—" : signed(chg) + "%"}</span>
            </div>;
          })}
        </div>
      </section>
    </>}
    <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
      <WatchlistPanel visible={visible} onSelect={onSelect} onQueued={() => setUpdates((value) => value + 1)} />
      <div className="min-w-0 space-y-3">
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-2 font-medium">发起研究</h2>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={onSearch} className="flex items-center gap-1 rounded bg-action px-2 py-1.5 text-gold-foreground"><Search className="size-3.5" /> 搜索股票</button>
            <button type="button" onClick={onRecap} className="rounded border px-2 py-1.5 hover:bg-accent">生成收盘复盘</button>
          </div>
        </section>
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-2 flex justify-between font-medium"><span>研究队列</span><button type="button" onClick={onResearch} className="text-xs text-gold">查看全部 <ArrowRight className="inline size-3" /></button></h2>
          {activeTasks?.length ? <ul className="divide-y">{activeTasks.map((task) => <li key={task.id} className="flex justify-between gap-2 py-1.5"><span>{task.name || task.code}</span><span className="text-xs text-muted-foreground">{task.status === "pending" ? "排队中" : "运行中"}</span></li>)}</ul>
            : activeTasks ? <p className="text-muted-foreground">当前没有排队或运行中的任务。</p> : <Skeleton className="h-8" />}
        </section>
        <section className="rounded-lg border bg-card p-3">
          <h2 className="mb-2 font-medium">最新研报</h2>
          {reports?.length ? <ul className="divide-y">{reports.slice(0, 5).map((report) => <li key={report.id}>
            <button type="button" className="flex w-full justify-between gap-2 py-1.5 text-left hover:text-gold" onClick={() => onReport(report.id)}>
              <span className="truncate">{report.name || report.ticker}</span><span className="shrink-0 text-xs text-muted-foreground">{report.created_at?.slice(0, 10)}</span>
            </button></li>)}</ul> : reports ? <p className="text-muted-foreground">还没有已保存的研报。</p> : <Skeleton className="h-8" />}
        </section>
      </div>
    </div>
  </div>;
}
