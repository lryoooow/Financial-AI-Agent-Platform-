import { useId, useMemo, useState } from "react";
import { Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { useChartAI } from "./useChartAI";

interface Point { time: string; price: number; avg: number; volume: number; }

export function IntradayChart({ data, prevClose, code }: { data: Point[]; prevClose: number; code?: string }) {
  const [hover, setHover] = useState<Point | null>(null);
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
  const { ai } = useChartAI(code, "intraday", hover, prevClose);
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
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={cleanData}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onMouseMove={(s: any) => { const i = s?.activeTooltipIndex; if (i != null && cleanData[i]) setHover(cleanData[i]); else setHover(null); }}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="time" tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} minTickGap={40} interval="preserveStartEnd" />
        <YAxis domain={[min - pad, max + pad]} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} width={52} tickFormatter={(v) => fmt(v, yDec)} orientation="right" />
        <ReferenceLine y={base} stroke="var(--muted-foreground)" strokeDasharray="3 4" strokeOpacity={0.5} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip up={up} prevClose={base} kind="intraday" ai={ai} />} />
        <Area type="monotone" dataKey="price" stroke={stroke} strokeWidth={1.8} fill={`url(#${gradId})`} isAnimationActive={false} dot={false} />
        <Line type="monotone" dataKey="avg" stroke="var(--gold)" strokeWidth={1.2} dot={false} strokeOpacity={0.8} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
