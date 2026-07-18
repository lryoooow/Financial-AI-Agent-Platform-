import { motion } from "motion/react";
import { fmt } from "../lib/format";

interface Row { label: string; price: number; vol: number; }

export function OrderBook({ asks, bids, prevClose }: { asks: Row[]; bids: Row[]; prevClose: number }) {
  const maxVol = Math.max(...asks.map((a) => a.vol), ...bids.map((b) => b.vol));
  const render = (rows: Row[], side: "ask" | "bid") =>
    rows.map((r, i) => {
      const up = r.price >= prevClose;
      const color = up ? "var(--up)" : "var(--down)";
      return (
        <div key={r.label} className="relative flex items-center justify-between px-2.5 py-1.5 text-xs">
          <motion.div
            className="absolute inset-y-0.5 rounded"
            style={{ background: side === "ask" ? "var(--up-soft)" : "var(--down-soft)", right: 0 }}
            initial={{ width: 0 }}
            animate={{ width: `${(r.vol / maxVol) * 100}%` }}
            transition={{ duration: 0.6, delay: i * 0.04, ease: [0.16, 1, 0.3, 1] }}
          />
          <span className="relative z-10 text-muted-foreground w-8">{r.label}</span>
          <span className="relative z-10 tabular" style={{ color }}>{fmt(r.price)}</span>
          <span className="relative z-10 tabular text-foreground/70 w-12 text-right">{r.vol}</span>
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
