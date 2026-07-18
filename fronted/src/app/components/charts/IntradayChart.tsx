import { useState } from "react";
import { Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { useChartAI } from "./useChartAI";

interface Point { time: string; price: number; avg: number; volume: number; }

export function IntradayChart({ data, prevClose, code }: { data: Point[]; prevClose: number; code?: string }) {
  const [hover, setHover] = useState<Point | null>(null);
  const { ai } = useChartAI(code, "intraday", hover, prevClose);
  const prices = data.map((d) => d.price);
  const min = Math.min(prevClose, ...prices);
  const max = Math.max(prevClose, ...prices);
  const pad = (max - min) * 0.25 || 1;
  const last = data[data.length - 1]?.price ?? prevClose;
  const up = last >= prevClose;
  const stroke = up ? "var(--up)" : "var(--down)";

  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={data}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onMouseMove={(s: any) => { const i = s?.activeTooltipIndex; if (i != null && data[i]) setHover(data[i]); else setHover(null); }}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="intradayFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="time" tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} minTickGap={40} interval="preserveStartEnd" />
        <YAxis domain={[min - pad, max + pad]} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} width={46} tickFormatter={(v) => fmt(v, 1)} orientation="right" />
        <ReferenceLine y={prevClose} stroke="var(--muted-foreground)" strokeDasharray="3 4" strokeOpacity={0.5} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip up={up} prevClose={prevClose} kind="intraday" ai={ai} />} />
        <Area type="monotone" dataKey="price" stroke={stroke} strokeWidth={1.8} fill="url(#intradayFill)" animationDuration={900} dot={false} />
        <Line type="monotone" dataKey="avg" stroke="var(--gold)" strokeWidth={1.2} dot={false} strokeOpacity={0.8} animationDuration={1100} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
