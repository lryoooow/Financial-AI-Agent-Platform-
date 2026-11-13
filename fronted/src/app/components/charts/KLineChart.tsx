import { useEffect, useId, useMemo, useState } from "react";
import { Bar, ComposedChart, Customized, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { ChartTooltip } from "./ChartTooltip";
import { ChartRead } from "./ChartRead";
import { VolumeChart } from "./VolumeChart";
import { TechnicalChart } from "./TechnicalChart";
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
  const syncId = useId();
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [study, setStudy] = useState<"volume" | "macd" | "rsi">("volume");
  useEffect(() => {
    setHoverIndex(null);
    setSelectedIndex(null);
  }, [code]);
  const cleanData = useMemo(() => {
    const seen = new Set<string>();
    const out: (Candle & { prevClose?: number })[] = [];
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
      const prevClose = out.length > 0 ? out[out.length - 1].close : 0;
      out.push({ ...d, prevClose });
    }
    return out;
  }, [data]);
  const index = hoverIndex ?? selectedIndex ?? cleanData.length - 1;
  const active = cleanData[index];
  const previous = cleanData[index - 1]?.close ?? 0;
  const lows = cleanData.map((d) => d.low);
  const highs = cleanData.map((d) => d.high);
  const min = lows.length ? Math.min(...lows) : 0;
  const max = highs.length ? Math.max(...highs) : 1;
  const pad = (max - min) * 0.08 || Math.max(max * 0.01, 0.05);
  const yDec = max > 0 && max < 5 ? 3 : max < 100 ? 2 : 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap gap-x-4 px-2 py-1 text-xs tabular-nums">
        {([["MA5", active?.ma5, "var(--gold-text)"], ["MA10", active?.ma10, "var(--chart-4)"],
          ["MA20", active?.ma20, "var(--chart-5)"], ["MA60", active?.ma60, "var(--muted-foreground)"]] as const)
          .map(([label, value, color]) => <span key={label} style={{ color }}>{label} {value == null ? "—" : fmt(value, 2)}</span>)}
      </div>
      <div className="min-h-0 flex-1">
      <ResponsiveContainer width="100%" height="100%">
      <ComposedChart
        data={cleanData}
        syncId={syncId}
        margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
        onMouseMove={(s: any) => {
          const i = s?.activeTooltipIndex;
          setHoverIndex(typeof i === "number" && cleanData[i] ? i : null);
        }}
        onClick={(state) => { if (typeof state?.activeTooltipIndex === "number") setSelectedIndex(state.activeTooltipIndex); }}
        onMouseLeave={() => setHoverIndex(null)}
      >
        <XAxis dataKey="date" scale="band" hide />
        <YAxis domain={[min - pad, max + pad]} orientation="right" width={52} tick={{ fontSize: 10, fill: "var(--muted-foreground)", fontFamily: "var(--font-mono)" }} axisLine={false} tickLine={false} tickFormatter={(v) => fmt(v, yDec)} />
        <Tooltip cursor={{ stroke: "var(--border)", strokeWidth: 1 }} content={<ChartTooltip kind="kline" prevClose={previous} />} />
        {hoverIndex != null && active && <ReferenceLine y={active.close} stroke="var(--muted-foreground)" strokeDasharray="3 4"
          label={{ value: fmt(active.close, yDec), position: "insideRight", fill: "var(--foreground)", fontSize: 11 }} />}
        {/* 透明 Bar 占位，建立 band 类目 scale */}
        <Bar dataKey="high" fill="transparent" isAnimationActive={false} />
        <Line type="monotone" dataKey="ma5" stroke="var(--gold)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="ma10" stroke="var(--chart-4)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="ma20" stroke="var(--chart-5)" dot={false} strokeWidth={1} connectNulls={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="ma60" stroke="var(--muted-foreground)" dot={false} strokeWidth={1} strokeDasharray="3 4" isAnimationActive={false} />
        <Customized component={Candles} />
      </ComposedChart>
      </ResponsiveContainer>
      </div>
      {study === "volume" ? <VolumeChart data={cleanData} syncId={syncId} labelKey="date" isUp={(point) => point.close >= point.open} />
        : <TechnicalChart data={cleanData} syncId={syncId} mode={study} />}
      <div className="flex gap-1 px-2 pb-1 text-xs">
        {([["volume", "成交量"], ["macd", "MACD"], ["rsi", "RSI"]] as const).map(([key, label]) => <button key={key} type="button"
          className="rounded px-2 py-0.5 aria-pressed:bg-accent" aria-pressed={study === key} onClick={() => setStudy(key)}>{label}</button>)}
      </div>
      <ChartRead code={code} kind="kline" point={cleanData[selectedIndex ?? cleanData.length - 1]} prevClose={cleanData[(selectedIndex ?? cleanData.length - 1) - 1]?.close ?? 0} />
    </div>
  );
}
