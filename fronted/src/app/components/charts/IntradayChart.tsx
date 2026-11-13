import { useEffect, useId, useMemo, useState } from "react";
import { Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { ChartRead } from "./ChartRead";
import { VolumeChart } from "./VolumeChart";

interface Point { time: string; price: number; avg: number; volume: number; }

export function IntradayChart({ data, prevClose, code }: { data: Point[]; prevClose: number; code?: string }) {
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => { setSelected(null); }, [code, prevClose]);
  const rawId = useId();
  const gradId = `intradayFill-${rawId.replace(/[^A-Za-z0-9_-]/g, "")}`;
  const cleanData = useMemo(() => {
    const seen = new Set<string>();
    const out: Point[] = [];
    for (const d of data || []) {
      if (!d?.time || seen.has(d.time) || !Number.isFinite(d.price) || !Number.isFinite(d.avg)) continue;
      seen.add(d.time);
      out.push(d);
    }
    return out;
  }, [data]);
  const prices = cleanData.map((d) => d.price);
  const base = Number.isFinite(prevClose) && prevClose > 0 ? prevClose : (prices[0] ?? 0);
  const min = prices.length ? Math.min(base, ...prices) : base;
  const max = prices.length ? Math.max(base, ...prices) : base;
  const pad = (max - min) * 0.25 || Math.max(base * 0.01, 0.05);
  const last = cleanData[cleanData.length - 1]?.price ?? base;
  const up = last >= base;
  const stroke = up ? "var(--up)" : "var(--down)";
  const yDec = base > 0 && base < 5 ? 3 : 2;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">
      <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={cleanData}
        syncId={rawId}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onClick={(state) => { if (typeof state?.activeTooltipIndex === "number") setSelected(state.activeTooltipIndex); }}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="time" scale="band" hide />
        <YAxis domain={[min - pad, max + pad]} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} width={52} tickFormatter={(v) => fmt(v, yDec)} orientation="right" />
        <ReferenceLine y={base} stroke="var(--muted-foreground)" strokeDasharray="3 4" strokeOpacity={0.5} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip up={up} prevClose={base} isFiveDay={!(Number.isFinite(prevClose) && prevClose > 0)} kind="intraday" />} />
        <Area type="monotone" dataKey="price" stroke={stroke} strokeWidth={1.8} fill={`url(#${gradId})`} isAnimationActive={false} dot={false} />
        <Line type="monotone" dataKey="avg" stroke="var(--gold)" strokeWidth={1.2} dot={false} strokeOpacity={0.8} isAnimationActive={false} />
      </ComposedChart>
      </ResponsiveContainer>
      </div>
      <VolumeChart data={cleanData} syncId={rawId} labelKey="time" isUp={(point, index) => point.price >= (cleanData[index - 1]?.price ?? base)} />
      <ChartRead code={code} kind="intraday" point={cleanData[selected ?? cleanData.length - 1]} prevClose={prevClose} />
    </div>
  );
}
