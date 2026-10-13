import { Area, Bar, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";

interface Point { time: string; price: number; avg: number; volume: number; }

function IntradayTooltip({ active, payload, prevClose }: any) {
  const row = payload?.[0]?.payload as Point | undefined;
  if (!active || !row) return null;
  const change = prevClose ? ((row.price - prevClose) / prevClose) * 100 : 0;
  return (
    <div className="rounded-lg border border-border bg-popover/95 px-3 py-2 text-xs shadow-xl backdrop-blur-sm">
      <div className="mb-1 text-muted-foreground tabular">{row.time}</div>
      <div className="flex justify-between gap-5 tabular"><span>现价</span><span style={{ color: change >= 0 ? "var(--up)" : "var(--down)" }}>{fmt(row.price, 2)}</span></div>
      <div className="flex justify-between gap-5 tabular text-muted-foreground"><span>均价</span><span>{fmt(row.avg, 2)}</span></div>
      <div className="mt-1 border-t border-border pt-1 tabular" style={{ color: change >= 0 ? "var(--up)" : "var(--down)" }}>
        较昨收 {change >= 0 ? "+" : ""}{fmt(change, 2)}%
      </div>
    </div>
  );
}

export function IntradayChart({ data, prevClose }: { data: Point[]; prevClose: number; code?: string }) {
  const reference = prevClose || data[0]?.price || 0;
  const deviation = Math.max(...data.map((row) => Math.abs(row.price - reference)), Math.abs(reference) * 0.002, 0.01);
  const domain: [number, number] = [reference - deviation * 1.12, reference + deviation * 1.12];
  const last = data[data.length - 1]?.price ?? reference;
  const stroke = last >= reference ? "var(--up)" : "var(--down)";
  const decimals = deviation < 0.1 ? 3 : 2;

  return (
    <div className="grid h-full min-h-[430px] grid-rows-[minmax(300px,4fr)_minmax(90px,1fr)] gap-1">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} syncId="intraday" margin={{ top: 10, right: 10, bottom: 0, left: 4 }}>
          <defs>
            <linearGradient id="intradayFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.3} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--border)" strokeOpacity={0.35} vertical={false} />
          <XAxis dataKey="time" tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} minTickGap={46} axisLine={false} tickLine={false} />
          <YAxis yAxisId="price" orientation="right" width={58} domain={domain} tickFormatter={(value) => fmt(value, decimals)} tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
          <YAxis yAxisId="percent" orientation="left" width={52} domain={domain} tickFormatter={(value) => reference ? `${(((value - reference) / reference) * 100).toFixed(2)}%` : "0%"} tick={{ fontSize: 9, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
          <ReferenceLine yAxisId="price" y={reference} stroke="var(--muted-foreground)" strokeDasharray="3 4" strokeOpacity={0.65} />
          <Tooltip content={<IntradayTooltip prevClose={reference} />} cursor={{ stroke: "var(--border)" }} />
          <Area yAxisId="price" type="monotone" dataKey="price" stroke={stroke} strokeWidth={2} fill="url(#intradayFill)" dot={false} isAnimationActive={false} />
          <Line yAxisId="price" type="monotone" dataKey="avg" stroke="var(--gold)" strokeWidth={1.2} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} syncId="intraday" margin={{ top: 0, right: 68, bottom: 0, left: 56 }}>
          <XAxis dataKey="time" hide />
          <YAxis hide />
          <Tooltip content={<IntradayTooltip prevClose={reference} />} />
          <Bar dataKey="volume" isAnimationActive={false}>
            {data.map((row) => <Cell key={row.time} fill={row.price >= reference ? "var(--up)" : "var(--down)"} fillOpacity={0.62} />)}
          </Bar>
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
