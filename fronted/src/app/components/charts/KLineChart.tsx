import { useState } from "react";
import { Bar, ComposedChart, Customized, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { useChartAI } from "./useChartAI";
import type { Candle } from "../../data/market";

// 用 recharts Customized 拿到坐标 scale，手绘蜡烛图（红涨绿跌）
function Candles(props: any) {
  const { xAxisMap, yAxisMap, data } = props;
  if (!xAxisMap || !yAxisMap) return null;
  const xAxis: any = Object.values(xAxisMap)[0];
  const yAxis: any = Object.values(yAxisMap)[0];
  const xScale = xAxis.scale;
  const yScale = yAxis.scale;
  const band = xScale.bandwidth ? xScale.bandwidth() : 8;
  const w = Math.max(2, Math.min(band * 0.62, 12));

  return (
    <g>
      {data.map((d: Candle, i: number) => {
        const cx = xScale(d.date) + band / 2;
        const up = d.close >= d.open;
        const color = up ? "var(--up)" : "var(--down)";
        const yHigh = yScale(d.high);
        const yLow = yScale(d.low);
        const yOpen = yScale(d.open);
        const yClose = yScale(d.close);
        const top = Math.min(yOpen, yClose);
        const h = Math.max(1, Math.abs(yClose - yOpen));
        return (
          <g key={i}>
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
  const { ai } = useChartAI(code, "kline", hover?.point ?? null, hover?.prev ?? data[0]?.open ?? 0);
  const lows = data.map((d) => d.low);
  const highs = data.map((d) => d.high);
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const pad = (max - min) * 0.08;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={data}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onMouseMove={(s: any) => {
          const i = s?.activeTooltipIndex;
          if (i != null && data[i]) setHover({ point: data[i], prev: data[Math.max(0, i - 1)].close });
          else setHover(null);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} minTickGap={28} />
        <YAxis domain={[min - pad, max + pad]} orientation="right" width={48} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} tickFormatter={(v) => fmt(v, 0)} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip kind="kline" ai={ai} />} />
        {/* 透明 Bar 占位，建立 band 类目 scale */}
        <Bar dataKey="high" fill="transparent" isAnimationActive={false} />
        <Line type="monotone" dataKey="ma5" stroke="var(--gold)" dot={false} strokeWidth={1} animationDuration={800} />
        <Line type="monotone" dataKey="ma10" stroke="var(--chart-4)" dot={false} strokeWidth={1} animationDuration={800} />
        <Line type="monotone" dataKey="ma20" stroke="var(--chart-5)" dot={false} strokeWidth={1} animationDuration={800} />
        <Customized component={(p: any) => <Candles {...p} data={data} />} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
