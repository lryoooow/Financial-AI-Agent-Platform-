import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { RadarRow } from "../../api/client";
import { fmt } from "../../lib/format";

function metric(value: number | null | undefined, unit = "", scale = 1) {
  return value == null || !Number.isFinite(value) ? "—" : fmt(value * scale) + unit;
}

export function RadarTable({ rows, selected, onToggle, onSelect }: {
  rows: RadarRow[]; selected: string[]; onToggle: (symbol: string) => void; onSelect: (symbol: string) => void;
}) {
  const [expanded, setExpanded] = useState("");
  const [category, setCategory] = useState("");
  const groups = Array.from(new Set(rows.flatMap((row) => row.groups)));
  const visible = category ? rows.filter((row) => row.groups.includes(category)) : rows;
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-1 text-xs">
      {["", ...groups].map((group) => <button key={group} type="button" aria-pressed={group === category} onClick={() => setCategory(group)}
        className="rounded border px-2 py-1 aria-pressed:bg-accent">{group || "全部"} {group ? rows.filter((row) => row.groups.includes(group)).length : rows.length}</button>)}
    </div>
    <div className="overflow-auto rounded border">
      <table className="financial-table min-w-[540px] text-xs">
        <thead><tr><th>标的</th><th>PE TTM 分位</th><th>营收同比</th><th>量比</th><th>60日回撤</th><th>72h 快讯</th></tr></thead>
        <tbody>{visible.map((row) => <Fragment key={row.symbol}>
          <tr>
            <td>
              <div className="flex items-center gap-2 whitespace-nowrap">
                <input type="checkbox" aria-label={"勾选 " + row.name} checked={selected.includes(row.symbol)} onChange={() => onToggle(row.symbol)} />
                <button type="button" aria-expanded={expanded === row.symbol} aria-label={"展开 " + row.name + " 事实"} onClick={() => setExpanded(expanded === row.symbol ? "" : row.symbol)}>
                  {expanded === row.symbol ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
                </button>
                <button type="button" onClick={() => onSelect(row.symbol)} className="text-gold">{row.name}</button>
              </div>
            </td>
            <td>{metric(row.valuation.pe_ttm_percentile, "%")}</td><td>{metric(row.earnings.revenue_yoy, "%")}</td>
            <td>{metric(row.price_volume.volume_ratio, "×")}</td><td>{metric(row.risk.max_drawdown, "%", 100)}</td><td>{row.events.count ?? "—"}</td>
          </tr>
          {expanded === row.symbol && <tr><td colSpan={6}>
            <div className="space-y-1 whitespace-normal text-left font-sans text-xs">
              <p>{row.groups.join(" · ")}</p>
              <p>PE TTM {metric(row.valuation.pe_ttm)} · PB {metric(row.valuation.pb)} · PB 分位 {metric(row.valuation.pb_percentile, "%")} · {row.valuation.as_of || "时点未知"}</p>
              <p>{row.earnings.period || "报告期未获取"} · 净利同比 {metric(row.earnings.profit_yoy, "%")} · ROE {metric(row.earnings.roe, "%")} · 经营现金流 {metric(row.earnings.operating_cashflow, " 亿", 1e-8)}</p>
              <p>收盘 {metric(row.price_volume.close)} · MA20 {metric(row.price_volume.ma20)} · RSI {metric(row.price_volume.rsi)} · {row.price_volume.as_of || "时点未知"}</p>
              <p>20日年化波动 {metric(row.risk.volatility_20d, "%", 100)} · 沪深300 Beta {metric(row.risk.beta)} · {row.risk.as_of || "时点未知"}</p>
              {row.events.items.map((item, i) => <p key={i}>「{item.title}」 · {item.time}</p>)}
              <p className="text-muted-foreground">{[row.valuation.provenance, row.earnings.provenance, row.earnings.cashflow_provenance, row.price_volume.provenance, row.risk.provenance?.benchmark, row.events.provenance]
                .filter(Boolean).map((entry) => [entry?.source, entry?.endpoint, entry?.fetched_at].filter(Boolean).join(" ")).join(" · ")}</p>
            </div>
          </td></tr>}
        </Fragment>)}</tbody>
      </table>
    </div>
  </div>;
}
