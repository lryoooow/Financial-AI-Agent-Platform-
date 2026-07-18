import { fmt } from "../../lib/format";

/**
 * 主题化图表 Tooltip（替代原 recharts 写死的 #14161c 深色弹窗）。
 * 用 bg-popover / border-border 等 token，浅色深色都正确。
 * `ai` 字段为"走势线 AI 速读"预留位（下一步接入 /api/chart/analyze）。
 *
 * recharts 会把 { active, payload, label } 作为 props 注入（克隆本元素时合并）。
 */
interface OwnProps {
  kind: "intraday" | "kline";
  up?: boolean;          // intraday：相对昨收的涨跌
  prevClose?: number;    // intraday：昨收，用于算偏离
  ai?: string;           // 走势线 AI 速读（可选）
}

const KLINE_LABEL: Record<string, string> = {
  open: "开", close: "收", high: "高", low: "低", ma5: "MA5", ma10: "MA10", ma20: "MA20",
};

export function ChartTooltip(ownProps: OwnProps) {
  const { active, payload, label } = ownProps as any;
  if (!active || !payload || !payload.length) return null;

  const { kind, up, prevClose, ai } = ownProps;
  const priceVal = payload.find((p: any) => p.dataKey === "price" || p.dataKey === "close")?.value;
  const pct = prevClose != null && typeof priceVal === "number"
    ? ((priceVal - prevClose) / prevClose) * 100
    : null;

  return (
    <div className="rounded-lg border border-border bg-popover/95 backdrop-blur-sm shadow-xl px-3 py-2 text-xs min-w-[128px]">
      <div className="text-muted-foreground mb-1 tabular">{label}</div>

      {payload.map((p: any) => (
        <div key={String(p.dataKey)} className="flex items-center justify-between gap-4 tabular leading-5">
          <span className="text-muted-foreground">
            {kind === "intraday"
              ? p.dataKey === "price" ? "现价" : p.dataKey === "avg" ? "均价" : String(p.dataKey)
              : (KLINE_LABEL[p.dataKey] ?? String(p.dataKey))}
          </span>
          <span className="text-foreground">{fmt(Number(p.value), 2)}</span>
        </div>
      ))}

      {kind === "intraday" && pct != null && (
        <div
          className="mt-1 pt-1 border-t border-border tabular text-[10px]"
          style={{ color: up ? "var(--up)" : "var(--down)" }}
        >
          较昨收 {pct >= 0 ? "+" : ""}{fmt(pct, 2)}%
        </div>
      )}

      {ai && (
        <div className="mt-1.5 pt-1.5 border-t border-border text-[10.5px] leading-snug text-foreground/80">
          <span className="text-gold font-medium">AI 速读 · </span>{ai}
        </div>
      )}
    </div>
  );
}
