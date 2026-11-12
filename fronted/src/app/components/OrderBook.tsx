import { motion } from "motion/react";
import { fmt } from "../lib/format";

interface Row { label: string; price: number; vol: number; lots?: number; }

export function OrderBook({ asks, bids, prevClose }: { asks: Row[]; bids: Row[]; prevClose: number }) {
  const getLots = (r: Row) => {
    const raw = typeof r.lots === "number" ? r.lots : Math.round((r.vol || 0) / 100);
    return Number.isFinite(raw) && raw > 0 ? raw : 0;
  };
  const maxLots = Math.max(1, ...asks.map(getLots), ...bids.map(getLots));
  const dec = prevClose > 0 && prevClose < 5 ? 3 : 2;
  const render = (rows: Row[], side: "ask" | "bid") =>
    rows.map((r, i) => {
      const lots = getLots(r);
      const validPrice = Number.isFinite(r.price) && r.price > 0;
      const up = r.price >= prevClose;
      const color = !validPrice ? "var(--muted-foreground)" : up ? "var(--up)" : "var(--down)";
      return (
        <div key={r.label} className="relative flex items-center justify-between px-2.5 py-1.5 text-xs">
          <motion.div
            className="absolute inset-y-0.5 rounded"
            style={{ background: side === "ask" ? "var(--up-soft)" : "var(--down-soft)", right: 0 }}
            initial={{ width: 0 }}
            animate={{ width: `${Math.min(100, (lots / maxLots) * 100)}%` }}
            transition={{ duration: 0.6, delay: i * 0.04, ease: [0.16, 1, 0.3, 1] }}
          />
          <span className="relative z-10 text-muted-foreground w-8">{r.label}</span>
          <span className="relative z-10 tabular" style={{ color }}>{validPrice ? fmt(r.price, dec) : "—"}</span>
          <span className="relative z-10 tabular text-foreground/70 w-14 text-right">{validPrice || lots > 0 ? lots : "—"}</span>
        </div>
      );
    });

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-3 py-2.5 border-b border-border flex items-center justify-between">
        <span className="text-sm">五档盘口</span>
        <span className="text-[10px] text-muted-foreground">手</span>
      </div>
      <div>{render(asks, "ask")}</div>
      <div className="my-0.5 mx-2.5 border-t border-dashed border-border" />
      <div>{render(bids, "bid")}</div>
    </div>
  );
}
