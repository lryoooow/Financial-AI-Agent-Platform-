import { useEffect, useMemo, useState } from "react";
import {
  Area,
  Bar,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  Customized,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { IndicatorPoint } from "../../api/client";
import type { Candle } from "../../data/market";
import { fmt } from "../../lib/format";

export type TechnicalIndicator = "MACD" | "RSI" | "KDJ" | "BOLL";
type ChartRow = Candle & Partial<IndicatorPoint>;

/** 均线条数随区间长度增加：5日只有 MA5，10日两条，20日三条，60/120/250 依次追加。
 *  后端按区间只下发 <= 区间长度的均线，这里按"数据里有没有值"决定画不画。 */
const MA_LINES = [
  { key: "ma5", label: "MA5", color: "var(--gold)" },
  { key: "ma10", label: "MA10", color: "var(--chart-4)" },
  { key: "ma20", label: "MA20", color: "var(--chart-5)" },
  { key: "ma60", label: "MA60", color: "var(--chart-2)" },
  { key: "ma120", label: "MA120", color: "var(--chart-1)" },
  { key: "ma250", label: "MA250", color: "var(--chart-3)" },
] as const;

function activeMaLines(rows: ChartRow[]) {
  return MA_LINES.filter((line) => rows.some((row) => typeof (row as any)[line.key] === "number"));
}

function Candles(props: any) {
  const { xAxisMap, yAxisMap, data } = props;
  if (!xAxisMap || !yAxisMap) return null;
  const xAxis: any = Object.values(xAxisMap)[0];
  const yAxis: any = Object.values(yAxisMap)[0];
  const xScale = xAxis.scale;
  const yScale = yAxis.scale;
  const band = xScale.bandwidth?.() || 10;
  // 长区间（如 250 日）band 会很窄，下限放到 1px，避免蜡烛互相重叠成一堵墙
  const width = Math.max(1, Math.min(band * 0.68, 18));
  return (
    <g>
      {data.map((row: ChartRow) => {
        const center = xScale(row.date) + band / 2;
        const color = row.close >= row.open ? "var(--up)" : "var(--down)";
        const openY = yScale(row.open);
        const closeY = yScale(row.close);
        return (
          <g key={row.date}>
            <line x1={center} x2={center} y1={yScale(row.high)} y2={yScale(row.low)} stroke={color} />
            <rect
              x={center - width / 2}
              y={Math.min(openY, closeY)}
              width={width}
              height={Math.max(1.5, Math.abs(closeY - openY))}
              fill={color}
              rx={0.6}
            />
          </g>
        );
      })}
    </g>
  );
}

function PriceTooltip({ active, payload }: any) {
  const row = payload?.[0]?.payload as ChartRow | undefined;
  if (!active || !row) return null;
  const change = row.open ? ((row.close - row.open) / row.open) * 100 : 0;
  return (
    <div className="rounded-lg border border-border bg-popover/95 px-3 py-2 text-xs shadow-xl backdrop-blur-sm">
      <div className="mb-1 text-muted-foreground tabular">{row.date}</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 tabular">
        <span className="text-muted-foreground">开 {fmt(row.open, 2)}</span>
        <span className="text-muted-foreground">高 {fmt(row.high, 2)}</span>
        <span className="text-muted-foreground">低 {fmt(row.low, 2)}</span>
        <span style={{ color: change >= 0 ? "var(--up)" : "var(--down)" }}>收 {fmt(row.close, 2)}</span>
      </div>
      <div className="mt-1 border-t border-border pt-1 tabular text-muted-foreground">
        日内 {change >= 0 ? "+" : ""}{fmt(change, 2)}% · 量 {Math.round(row.volume).toLocaleString("zh-CN")}
      </div>
      {activeMaLines([row]).length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 border-t border-border pt-1 tabular">
          {activeMaLines([row]).map((line) => (
            <span key={line.key} style={{ color: line.color }}>{line.label} {fmt((row as any)[line.key], 2)}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function indicatorChart(indicator: TechnicalIndicator, data: ChartRow[]) {
  if (indicator === "RSI") {
    return (
      <>
        <ReferenceLine y={70} stroke="var(--up)" strokeDasharray="3 3" strokeOpacity={0.45} />
        <ReferenceLine y={30} stroke="var(--down)" strokeDasharray="3 3" strokeOpacity={0.45} />
        <Line type="monotone" dataKey="rsi14" stroke="var(--gold)" dot={false} isAnimationActive={false} />
      </>
    );
  }
  if (indicator === "KDJ") {
    return (
      <>
        <Line type="monotone" dataKey="k" stroke="var(--gold)" dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="d" stroke="var(--chart-4)" dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="j" stroke="var(--chart-5)" dot={false} isAnimationActive={false} />
      </>
    );
  }
  if (indicator === "BOLL") {
    return (
      <>
        <Line type="monotone" dataKey="bollUpper" stroke="var(--up)" dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="bollMid" stroke="var(--gold)" dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="bollLower" stroke="var(--down)" dot={false} isAnimationActive={false} />
      </>
    );
  }
  return (
    <>
      <Bar dataKey="macd" isAnimationActive={false}>
        {data.map((row) => <Cell key={row.date} fill={(row.macd ?? 0) >= 0 ? "var(--up)" : "var(--down)"} />)}
      </Bar>
      <Line type="monotone" dataKey="dif" stroke="var(--gold)" dot={false} isAnimationActive={false} />
      <Line type="monotone" dataKey="dea" stroke="var(--chart-4)" dot={false} isAnimationActive={false} />
    </>
  );
}

export function KLineChart({
  data,
  indicators = [],
  indicator,
  onIndicatorChange,
  onVisibleRangeChange,
}: {
  data: Candle[];
  indicators?: IndicatorPoint[];
  code?: string;
  indicator: TechnicalIndicator;
  onIndicatorChange: (indicator: TechnicalIndicator) => void;
  onVisibleRangeChange?: (start: string, end: string) => void;
}) {
  const merged = useMemo(() => {
    const byDate = new Map(indicators.map((row) => [row.date, row]));
    return data.map((row) => ({ ...row, ...(byDate.get(row.date) || {}) }));
  }, [data, indicators]);
  // 默认展示所选区间的全部 K 线（选 250 日就看满 250 日），需要放大再拖下方 Brush
  const [windowRange, setWindowRange] = useState(() => ({ start: 0, end: Math.max(0, merged.length - 1) }));

  useEffect(() => {
    setWindowRange({ start: 0, end: Math.max(0, merged.length - 1) });
  }, [merged.length]);

  const visible = merged.slice(windowRange.start, windowRange.end + 1);
  useEffect(() => {
    if (visible.length) onVisibleRangeChange?.(visible[0].date, visible[visible.length - 1].date);
  }, [onVisibleRangeChange, visible[0]?.date, visible[visible.length - 1]?.date]);

  if (!visible.length) return null;
  const low = Math.min(...visible.map((row) => row.low));
  const high = Math.max(...visible.map((row) => row.high));
  const spread = Math.max(high - low, Math.abs(high) * 0.002, 0.01);
  const priceDomain: [number, number] = [low - spread * 0.08, high + spread * 0.08];
  const decimals = spread < 0.2 ? 3 : spread < 10 ? 2 : 1;
  const maLines = activeMaLines(visible);

  return (
    <div className="grid h-full min-h-[520px] grid-rows-[minmax(270px,5fr)_minmax(80px,1.35fr)_minmax(120px,2fr)_38px] gap-1">
      <div className="relative min-h-0">
        <div className="pointer-events-none absolute left-3 top-1 z-10 flex flex-wrap gap-2 text-[10px]">
          {maLines.map((line) => (
            <span key={line.key} className="flex items-center gap-1" style={{ color: line.color }}>
              <span className="inline-block h-[2px] w-3 rounded" style={{ background: line.color }} />
              {line.label}
            </span>
          ))}
        </div>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={visible} syncId="kline" margin={{ top: 18, right: 10, bottom: 0, left: 4 }}>
          <CartesianGrid stroke="var(--border)" strokeOpacity={0.35} vertical={false} />
          <XAxis dataKey="date" hide />
          <YAxis orientation="right" width={58} domain={priceDomain} tickFormatter={(value) => fmt(value, decimals)} tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
          <Tooltip cursor={{ stroke: "var(--border)" }} content={<PriceTooltip />} />
          <Bar dataKey="open" fill="transparent" isAnimationActive={false} />
          {maLines.map((line) => (
            <Line key={line.key} type="monotone" dataKey={line.key} stroke={line.color} strokeWidth={1.2} dot={false} connectNulls={false} isAnimationActive={false} />
          ))}
          <Customized component={(props: any) => <Candles {...props} data={visible} />} />
        </ComposedChart>
      </ResponsiveContainer>
      </div>

      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={visible} syncId="kline" margin={{ top: 0, right: 10, bottom: 0, left: 4 }}>
          <XAxis dataKey="date" hide />
          <YAxis orientation="right" width={58} tickFormatter={(value) => `${Math.round(value / 10000)}万`} tick={{ fontSize: 9, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
          <Tooltip content={<PriceTooltip />} />
          <Bar dataKey="volume" isAnimationActive={false}>
            {visible.map((row) => <Cell key={row.date} fill={row.close >= row.open ? "var(--up)" : "var(--down)"} fillOpacity={0.65} />)}
          </Bar>
        </ComposedChart>
      </ResponsiveContainer>

      <div className="relative min-h-0">
        <div className="absolute left-2 top-1 z-10 flex gap-1">
          {(["MACD", "RSI", "KDJ", "BOLL"] as TechnicalIndicator[]).map((item) => (
            <button key={item} onClick={() => onIndicatorChange(item)} className={`rounded px-1.5 py-0.5 text-[10px] ${indicator === item ? "bg-gold text-[#17140a]" : "bg-secondary text-muted-foreground"}`}>
              {item}
            </button>
          ))}
        </div>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={visible} syncId="kline" margin={{ top: 24, right: 10, bottom: 0, left: 4 }}>
            <CartesianGrid stroke="var(--border)" strokeOpacity={0.25} vertical={false} />
            <XAxis dataKey="date" tick={{ fontSize: 9, fill: "var(--muted-foreground)" }} minTickGap={32} axisLine={false} tickLine={false} />
            <YAxis orientation="right" width={58} domain={indicator === "RSI" || indicator === "KDJ" ? [0, 100] : ["auto", "auto"]} tick={{ fontSize: 9, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
            <Tooltip content={<PriceTooltip />} />
            {indicatorChart(indicator, visible)}
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={merged} margin={{ top: 0, right: 68, bottom: 0, left: 4 }}>
          <XAxis dataKey="date" hide />
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <Area dataKey="close" stroke="var(--gold)" fill="var(--gold)" fillOpacity={0.12} isAnimationActive={false} />
          <Brush
            dataKey="date"
            height={28}
            travellerWidth={8}
            startIndex={windowRange.start}
            endIndex={windowRange.end}
            stroke="var(--gold)"
            fill="var(--secondary)"
            onChange={(range) => {
              if (typeof range.startIndex === "number" && typeof range.endIndex === "number") {
                setWindowRange({ start: range.startIndex, end: range.endIndex });
              }
            }}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
