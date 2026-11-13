import { Bar, Cell, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Candle } from "../../data/market";
import { fmt } from "../../lib/format";

export function technicalSeries(data: Candle[]) {
  let ema12 = data[0]?.close ?? 0, ema26 = ema12, signal = 0;
  let averageGain = 0, averageLoss = 0;
  return data.map((point, i) => {
    ema12 += (point.close - ema12) * 2 / 13;
    ema26 += (point.close - ema26) * 2 / 27;
    const macd = ema12 - ema26;
    signal = i === 25 ? macd : signal + (macd - signal) * 2 / 10;
    if (i > 0) {
      const diff = point.close - data[i - 1].close;
      const gain = Math.max(0, diff), loss = Math.max(0, -diff);
      averageGain = i <= 14 ? averageGain + gain / 14 : (averageGain * 13 + gain) / 14;
      averageLoss = i <= 14 ? averageLoss + loss / 14 : (averageLoss * 13 + loss) / 14;
    }
    const rsi = i < 14 ? null : averageLoss > 0 ? 100 - 100 / (1 + averageGain / averageLoss) : averageGain > 0 ? 100 : 50;
    return { date: point.date, macd: i >= 25 ? macd : null, signal: i >= 33 ? signal : null,
      histogram: i >= 33 ? 2 * (macd - signal) : null, rsi };
  });
}

export function TechnicalChart({ data, mode, syncId }: { data: Candle[]; mode: "macd" | "rsi"; syncId: string }) {
  const values = technicalSeries(data);
  const isRsi = mode === "rsi";
  const available = values.some((value) => isRsi ? value.rsi != null : value.macd != null);
  return <div className="h-[28%] min-h-[70px] shrink-0 border-t">
    {!available ? <p className="p-2 text-xs text-muted-foreground">样本不足，无法计算该指标。</p> :
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={values} syncId={syncId} margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
          <XAxis dataKey="date" scale="band" axisLine={false} tickLine={false} minTickGap={40} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} />
          <YAxis width={52} orientation="right" domain={isRsi ? [0, 100] : ["auto", "auto"]} tickCount={3}
            tickFormatter={(value) => fmt(value, 1)} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false} />
          <Tooltip formatter={(value) => fmt(Number(value))}
            contentStyle={{ background: "var(--popover)", borderColor: "var(--border)", color: "var(--foreground)", fontSize: 12 }} />
          {isRsi ? <>
            <ReferenceLine y={30} stroke="var(--muted-foreground)" strokeDasharray="3 4" />
            <ReferenceLine y={70} stroke="var(--muted-foreground)" strokeDasharray="3 4" />
            <Line dataKey="rsi" name="RSI 14" stroke="var(--gold-text)" dot={false} isAnimationActive={false} />
          </> : <>
            <ReferenceLine y={0} stroke="var(--muted-foreground)" strokeOpacity={0.45} />
            <Bar dataKey="histogram" name="MACD 柱" isAnimationActive={false}>
              {values.map((point) => <Cell key={point.date} fill={(point.histogram ?? 0) >= 0 ? "var(--up)" : "var(--down)"} />)}
            </Bar>
            <Line dataKey="macd" name="DIF" stroke="var(--gold-text)" dot={false} isAnimationActive={false} />
            <Line dataKey="signal" name="DEA" stroke="var(--chart-4)" dot={false} isAnimationActive={false} />
          </>}
        </ComposedChart>
      </ResponsiveContainer>}
  </div>;
}
