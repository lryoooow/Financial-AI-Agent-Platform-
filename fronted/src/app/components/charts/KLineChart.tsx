import { useMemo, useState } from "react";
import { Bar, ComposedChart, Customized, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { useChartAI } from "./useChartAI";
import type { Candle } from "../../data/market";

// 用 recharts Customized 拿到坐标 scale，手绘蜡烛图（红涨绿跌）
function Candles(props: any) {
  const { xAxisMap, yAxisMap, data } = props;
  if (!xAxisMap || !yAxisMap || !Array.isArray(data)) return null;
  const xAxis: any = Object.values(xAxisMap)[0];
  const yAxis: any = Object.values(yAxisMap)[0];
  if (!xAxis?.scale || !yAxis?.scale) return null;
  const xScale = xAxis.scale;
  const yScale = yAxis.scale;
  const band = xScale.bandwidth ? xScale.bandwidth() : 8;
  const w = Math.max(2, Math.min(band * 0.62, 12));

  return (
    <g>
      {data.map((d: Candle) => {
        const xVal = xScale(d.date);
        if (!Number.isFinite(xVal)) return null;
        const cx = xVal + band / 2;
        const up = d.close >= d.open;
        const color = up ? "var(--up)" : "var(--down)";
        const yHigh = yScale(d.high);
        const yLow = yScale(d.low);
        const yOpen = yScale(d.open);
        const yClose = yScale(d.close);
        if (![yHigh, yLow, yOpen, yClose].every(Number.isFinite)) return null;
        const top = Math.min(yOpen, yClose);
        const h = Math.max(1, Math.abs(yClose - yOpen));
        return (
          <g key={d.date}>
            <line x1={cx} x2={cx} y1={yHigh} y2={yLow} stroke={color} strokeWidth={1} />
            <rect x={cx - w / 2} y={top} width={w} height={h} fill={color} rx={0.5} />
          </g>
        );
      })}
    </g>
  );
}

export function KLineChart({ data, code }: { data: Candle[]; code?: string }) {
  const [hover, setHover] = useState<{ point: Candle; prev: number } | null>(null);
  const cleanData = useMemo(() => {
    const seen = new Set<string>();
    const out: Candle[] = [];
    for (const d of data || []) {
      if (
        !d?.date ||
        seen.has(d.date) ||
        !Number.isFinite(d.open) ||
        !Number.isFinite(d.close) ||
        !Number.isFinite(d.high) ||
        !Number.isFinite(d.low)
      ) {
        continue;
      }
      seen.add(d.date);
      out.push(d);
    }
    return out;
  }, [data]);
  const { ai } = useChartAI(code, "kline", hover?.point ?? null, hover?.prev ?? cleanData[0]?.open ?? 0);
  const lows = cleanData.map((d) => d.low);
  const highs = cleanData.map((d) => d.high);
  const min = lows.length ? Math.min(...lows) : 0;
  const max = highs.length ? Math.max(...highs) : 1;
  const pad = (max - min) * 0.08 || Math.max(max * 0.01, 0.05);
  const yDec = max > 0 && max < 5 ? 3 : max < 100 ? 2 : 0;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={cleanData}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onMouseMove={(s: any) => {
          const i = s?.activeTooltipIndex;
          if (i != null && cleanData[i]) setHover({ point: cleanData[i], prev: i > 0 ? cleanData[i - 1].close : cleanData[i].open });
          else setHover(null);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} minTickGap={28} />
        <YAxis domain={[min - pad, max + pad]} orientation="right" width={52} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} tickFormatter={(v) => fmt(v, yDec)} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip kind="kline" prevClose={hover?.prev} ai={ai} />} />
        {/* 透明 Bar 占位，建立 band 类目 scale */}
        <Bar dataKey="high" fill="transparent" isAnimationActive={false} />
        <Line type="monotone" dataKey="ma5" stroke="var(--gold)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="ma10" stroke="var(--chart-4)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="ma20" stroke="var(--chart-5)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Customized component={Candles} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
