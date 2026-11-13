import { X } from "lucide-react";
import type { ToolArtifact } from "../../api/client";
import type { Candle } from "../../data/market";
import { fmt, signed, trendClass } from "../../lib/format";
import { KLineChart } from "../charts/KLineChart";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function pctText(value: unknown, scale = 100): string {
  return typeof value === "number" && Number.isFinite(value) ? (value * scale).toFixed(2) + "%" : "—";
}

function numText(value: unknown, decimals = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? fmt(value, decimals) : value != null && value !== "" ? String(value) : "—";
}

const RISK_LABELS: [string, string, ((v: unknown) => string)?][] = [
  ["annualized_volatility", "年化波动率", (v) => pctText(v, 100)],
  ["volatility_20d", "20日年化波动率", (v) => pctText(v, 100)],
  ["max_drawdown", "最大回撤", (v) => pctText(v, 100)],
  ["beta", "基准 Beta", (v) => numText(v, 2)],
  ["var_95", "日度 VaR (95%)", (v) => pctText(v, 100)],
  ["cvar_95", "日度 CVaR (95%)", (v) => pctText(v, 100)],
  ["sharpe", "夏普比率", (v) => numText(v, 2)],
  ["sortino", "索提诺比率", (v) => numText(v, 2)],
  ["window_days", "样本交易日", (v) => numText(v, 0)],
  ["benchmark", "基准指数"],
  ["as_of", "截至交易日"],
];

export function ArtifactDrawer({ entry, onClose }: { entry: ToolArtifact; onClose: () => void }) {
  const data = entry.artifact;
  const financials = record(data.financials);
  const rows = Array.isArray(financials.rows) ? financials.rows : [];
  const years = Array.isArray(financials.years) ? financials.years : [];
  const source = record(data.provenance);
  const rawItems = Array.isArray(data.items) ? data.items : [];
  const webResults = Array.isArray(data.results) ? data.results : [];
  const isIndustryItems = entry.name === "search_industry" && rawItems.some((item) => item && typeof item === "object" && ("pe_ttm" in item || "pb" in item || "industry_name" in item));
  const docItems = isIndustryItems ? [] : [...rawItems, ...webResults];
  const radar = Array.isArray(data.rows) ? data.rows : [];
  const prices = entry.name === "get_kline" && Array.isArray(data.data) ? data.data as Candle[] : [];
  const indicators = entry.name === "get_technical_indicators"
    ? (Array.isArray(data.indicators) ? data.indicators : Array.isArray(data.data) ? data.data : [])
    : [];
  const riskMetrics = entry.name === "compute_risk_metrics" ? record(data.metrics ?? data) : {};
  const hasRiskMetrics = entry.name === "compute_risk_metrics" && Object.keys(riskMetrics).length > 0;
  const isMarketOverview = entry.name === "market_overview";
  const overviewIndices = isMarketOverview && Array.isArray(data.indices) ? data.indices : [];
  const overviewBreadth = isMarketOverview ? record(data.breadth) : {};
  const overviewIndustries = isMarketOverview && Array.isArray(data.industries) ? data.industries : [];
  const fields = Object.entries(data).filter(([name, value]) =>
    !["text", "model_text", "error_code"].includes(name) && (typeof value === "number" || typeof value === "string")
  );
  return (
    <aside className="h-full overflow-auto border-l bg-background" aria-label="数据详情">
      <header className="sticky top-0 z-10 flex h-11 items-center justify-between border-b bg-popover px-3">
        <h2 className="truncate font-medium">{entry.label}</h2>
        <button type="button" aria-label="关闭数据详情" className="rounded p-1 hover:bg-accent" onClick={onClose}><X className="size-4" /></button>
      </header>
      <div className="space-y-3 p-3">
        {typeof source.source === "string" && <p className="text-xs text-muted-foreground">{source.source} {String(source.fetched_at || "")}</p>}
        {!!prices.length && <div className="h-80"><KLineChart data={prices} code={typeof data.code === "string" ? data.code : undefined} /></div>}

        {hasRiskMetrics && (
          <div className="overflow-auto rounded border">
            <table className="financial-table text-xs">
              <thead><tr><th>风险指标</th><th>数值</th></tr></thead>
              <tbody>
                {RISK_LABELS.filter(([key]) => riskMetrics[key] !== undefined && riskMetrics[key] !== null).map(([key, label, renderFn]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td>{renderFn ? renderFn(riskMetrics[key]) : String(riskMetrics[key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!!indicators.length && (
          <div className="overflow-auto rounded border">
            <table className="financial-table text-xs">
              <thead><tr><th>技术指标</th><th>数值</th><th>状态</th></tr></thead>
              <tbody>
                {indicators.map((ind: any, index: number) => (
                  <tr key={index}>
                    <td>{String(ind?.name ?? "—")}</td>
                    <td>{String(ind?.value ?? "—")}</td>
                    <td>{String(ind?.signal ?? "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {isIndustryItems && !!rawItems.length && (
          <div className="overflow-auto rounded border">
            <table className="financial-table text-xs">
              <thead><tr><th>行业 / 板块</th><th>涨跌幅</th><th>PE (TTM)</th><th>PB</th><th>股息率</th></tr></thead>
              <tbody>
                {rawItems.map((item: any, index: number) => {
                  const chg = typeof item?.change_pct === "number" ? item.change_pct : typeof item?.changePct === "number" ? item.changePct : null;
                  return (
                    <tr key={index}>
                      <td>{String(item?.name || item?.industry_name || item?.code || "—")}</td>
                      <td className={chg != null ? trendClass(chg) : ""}>{chg != null ? `${signed(chg)}%` : "—"}</td>
                      <td>{numText(item?.pe_ttm ?? item?.pe, 2)}</td>
                      <td>{numText(item?.pb, 2)}</td>
                      <td>{typeof item?.dividend_yield === "number" ? fmt(item.dividend_yield, 2) + "%" : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {isMarketOverview && (
          <div className="space-y-3">
            {!!overviewIndices.length && (
              <div className="overflow-auto rounded border">
                <table className="financial-table text-xs">
                  <thead><tr><th>宽基指数</th><th>点位</th><th>涨跌幅</th></tr></thead>
                  <tbody>
                    {overviewIndices.map((idx: any, i: number) => {
                      const chg = typeof idx?.changePct === "number" ? idx.changePct : null;
                      return (
                        <tr key={i}>
                          <td>{String(idx?.name || idx?.code || "—")}</td>
                          <td>{numText(idx?.price, 2)}</td>
                          <td className={chg != null ? trendClass(chg) : ""}>{chg != null ? `${signed(chg)}%` : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {Object.keys(overviewBreadth).length > 0 && (
              <div className="rounded border bg-card p-2.5 text-xs space-y-1">
                <div className="flex justify-between font-mono tabular">
                  <span className="text-up">上涨 {String(overviewBreadth.up ?? "—")}</span>
                  <span className="text-muted-foreground">平盘 {String(overviewBreadth.flat ?? "—")}</span>
                  <span className="text-down">下跌 {String(overviewBreadth.down ?? "—")}</span>
                </div>
                <div className="text-muted-foreground tabular">
                  涨停 {String(overviewBreadth.limit_up ?? "—")} · 跌停 {String(overviewBreadth.limit_down ?? "—")}
                  {typeof overviewBreadth.turnover_yuan === "number" ? ` · 两市成交 ${fmt(overviewBreadth.turnover_yuan / 1e8, 1)} 亿` : ""}
                </div>
              </div>
            )}
            {!!overviewIndustries.length && (
              <div className="overflow-auto rounded border">
                <table className="financial-table text-xs">
                  <thead><tr><th>申万行业</th><th>涨跌幅</th><th>主力净流入</th></tr></thead>
                  <tbody>
                    {overviewIndustries.slice(0, 12).map((ind: any, i: number) => {
                      const chg = typeof ind?.changePct === "number" ? ind.changePct : null;
                      return (
                        <tr key={i}>
                          <td>{String(ind?.name || ind?.code || "—")}</td>
                          <td className={chg != null ? trendClass(chg) : ""}>{chg != null ? `${signed(chg)}%` : "—"}</td>
                          <td>{typeof ind?.net_flow_yuan === "number" ? fmt(ind.net_flow_yuan / 1e8, 1) + " 亿" : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {!!rows.length && <div className="overflow-auto rounded border">
          <table className="financial-table text-xs">
            <thead><tr><th>指标</th>{years.map((year, index) => <th key={index}>{String(year)}</th>)}</tr></thead>
            <tbody>{rows.map((row, index) => <tr key={index}><td>{String(row.name)}</td>{row.values?.map((value: unknown, column: number) => <td key={column}>{String(value ?? "—")}</td>)}</tr>)}</tbody>
          </table>
        </div>}
        {!!docItems.length && <ul className="space-y-2">{docItems.map((item: any, index: number) => <li key={index} className="rounded border p-2">
          <p className="font-medium">{String(item?.title || item?.source || "资料片段")}</p>
          <p className="text-xs text-muted-foreground">{String(item?.as_of || item?.time || item?.published_date || item?.date || "")}</p>
          {(item?.text || item?.snippet || item?.content) && <p className="mt-1 whitespace-pre-wrap text-sm">{String(item.text || item.snippet || item.content)}</p>}
          {typeof item?.url === "string" && /^https?:\/\//i.test(item.url) && <a className="text-gold underline text-xs" href={item.url} target="_blank" rel="noreferrer">查看来源</a>}
        </li>)}</ul>}
        {!!radar.length && <div className="overflow-auto rounded border"><table className="financial-table text-xs">
          <thead><tr><th>标的</th><th>PE</th><th>ROE</th><th>最大回撤</th></tr></thead>
          <tbody>{radar.map((item, index) => <tr key={index}><td>{String(item.name || item.symbol)}</td>
            <td>{item.valuation?.pe_ttm ?? "—"}</td><td>{item.earnings?.roe ?? "—"}</td>
            <td>{typeof item.risk?.max_drawdown === "number" ? (item.risk.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
          </tr>)}</tbody>
        </table></div>}
        {typeof data.text === "string" && !docItems.length && <p className="whitespace-pre-wrap text-sm">{data.text}</p>}
        {!!fields.length && <dl className="divide-y rounded border">{fields.map(([name, value]) => <div key={name} className="flex justify-between gap-2 px-2 py-1 text-xs"><dt className="text-muted-foreground">{name}</dt><dd className="text-right font-mono">{String(value)}</dd></div>)}</dl>}
      </div>
    </aside>
  );
}
