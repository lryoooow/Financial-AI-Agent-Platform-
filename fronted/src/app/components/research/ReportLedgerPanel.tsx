import { useEffect, useState } from "react";
import { api, type FactSnapshot, type ReportLedger } from "../../api/client";
import { fmt, signed } from "../../lib/format";
import { Skeleton } from "../ui/skeleton";

const FIELDS: [keyof FactSnapshot, string, string?][] = [
  ["price_as_of", "行情时点"], ["report_price", "股价", " 元"], ["report_pe_ttm", "PE TTM", " 倍"],
  ["report_pb", "PB", " 倍"], ["hs300_close", "沪深300", " 点"], ["hs300_as_of", "沪深300 时点"],
  ["fin_period", "报告期"], ["revenue_yoy", "营收同比", "%"], ["profit_yoy", "净利同比", "%"],
];

function cell(value: FactSnapshot[keyof FactSnapshot], unit = "") {
  if (typeof value === "number" && Number.isFinite(value)) return fmt(value) + unit;
  return typeof value === "string" && value ? value : "—";
}

export function ReportLedgerPanel({ id }: { id: string }) {
  const [ledger, setLedger] = useState<ReportLedger>();
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLedger(undefined);
    setError("");
    api.ledger(id, controller.signal).then(setLedger).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => controller.abort();
  }, [id, refresh]);
  return <section className="space-y-4 p-4">
    <div className="flex justify-between gap-2">
      <h2 className="font-medium">跨期事实对账单</h2>
      <button type="button" onClick={() => setRefresh((value) => value + 1)} className="text-gold">刷新当前数据</button>
    </div>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!ledger && !error && <Skeleton className="h-48" />}
    {ledger && <>
      {(ledger.legacy || ledger.message) && <p className="text-muted-foreground">{ledger.message || "历史研报从原始记录读取当时可核验的数据。"}</p>}
      <div className="overflow-auto rounded border">
        <table className="financial-table text-xs">
          <thead><tr><th>事实字段</th><th>研报时点</th><th>当前数据</th></tr></thead>
          <tbody>{FIELDS.map(([key, label, unit]) => <tr key={key}>
            <td>{label}</td><td>{cell(ledger.snapshot[key], unit)}</td><td>{cell(ledger.current[key], unit)}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {([["现价涨跌（不复权）", ledger.comparison.price_change_pct, "%"], ["相对沪深300（前复权收盘）", ledger.comparison.excess_pp, " pp"],
          ["期间最大回撤（前复权）", ledger.comparison.max_drawdown_pct, "%"], ["PE TTM 变化", ledger.comparison.pe_change_pct, "%"],
          ["PB 变化", ledger.comparison.pb_change_pct, "%"]] as const).map(([label, value, unit]) => <div key={label} className="rounded border p-2">
            <p className="text-xs text-muted-foreground">{label}</p><p className="font-mono tabular-nums">{value == null ? "—" : signed(value) + unit}</p>
          </div>)}
      </div>
      {ledger.comparison.return_start && <p className="text-xs text-muted-foreground">超额收益区间 {ledger.comparison.return_start} 至 {ledger.comparison.return_end}</p>}
      {ledger.comparison.drawdown_as_of && <p className="text-xs text-muted-foreground">回撤计算截至 {ledger.comparison.drawdown_as_of}</p>}
      {ledger.comparison.new_financial_period && <p className="text-gold">已发现后续报告期数据。</p>}
      <div className="text-xs text-muted-foreground">
        {[ledger.snapshot, ledger.current].flatMap((group, index) => Object.entries(group.provenance || {}).flatMap(([key, value]) =>
          value?.source ? [<p key={index + key}>{index ? "当前" : "研报"} · {value.source} {value.endpoint} {value.fetched_at}</p>] : []))}
      </div>
    </>}
  </section>;
}
