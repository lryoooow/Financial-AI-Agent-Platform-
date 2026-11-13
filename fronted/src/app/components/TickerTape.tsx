import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { api, type MarketOverview } from "../api/client";
import { fmt, signed, trendClass } from "../lib/format";

export function TickerTape({ market, onMarket }: { market?: MarketOverview; onMarket: (value: MarketOverview) => void }) {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => {
      if (!document.hidden) api.marketOverview(controller.signal).then(onMarket).catch(() => {});
    };
    refresh();
    const timer = setInterval(refresh, 45_000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [onMarket]);
  return (
    <div className="flex h-7 shrink-0 items-center border-b bg-secondary px-2 text-xs">
      <button type="button" title={collapsed ? "展开指数" : "收起指数"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)} className="flex h-full items-center gap-1 px-2 text-muted-foreground">
        指数 {collapsed ? <ChevronDown className="size-3" /> : <ChevronUp className="size-3" />}
      </button>
      {!collapsed && <div className="flex min-w-0 flex-1 items-center gap-4 overflow-x-auto whitespace-nowrap px-2">
        {market?.indices.map((index) => (
          <span key={index.code} title={index.as_of || "数据时点未知"} className="inline-flex items-center gap-1.5">
            {index.name}
            <span className={"font-mono tabular " + trendClass(index.changePct ?? 0)}>
              {index.price == null ? "—" : fmt(index.price)} {index.changePct == null ? "" : signed(index.changePct) + "%"}
            </span>
          </span>
        )) ?? <span className="h-2 w-36 animate-pulse rounded bg-muted" aria-label="加载指数" />}
        {market?.breadth && <span className="ml-auto flex gap-3 tabular">
          <span>涨 {market.breadth.up ?? "—"}</span><span>跌 {market.breadth.down ?? "—"}</span>
          <span>沪深成交 {market.breadth.turnover_yuan == null ? "—" : fmt(market.breadth.turnover_yuan / 1e8, 1) + " 亿"}</span>
        </span>}
      </div>}
    </div>
  );
}
