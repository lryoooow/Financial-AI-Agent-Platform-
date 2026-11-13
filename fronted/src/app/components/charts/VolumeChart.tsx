import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export function VolumeChart<T extends { volume: number }>({
  data, labelKey, syncId, isUp,
}: { data: T[]; labelKey: "date" | "time"; syncId: string; isUp: (point: T, index: number) => boolean }) {
  return <div className="h-[28%] min-h-[55px] shrink-0 border-t border-border" aria-label="成交量">
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} syncId={syncId} margin={{ top: 3, right: 8, bottom: 0, left: 4 }}>
        <XAxis dataKey={labelKey} scale="band" tickFormatter={(label) => String(label).slice(-11)} minTickGap={34} axisLine={false} tickLine={false}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} />
        <YAxis width={52} orientation="right" axisLine={false} tickLine={false} tickCount={3}
          tickFormatter={(volume) => volume >= 10000 ? (volume / 10000).toFixed(1) + "万" : String(volume)}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }} />
        <Tooltip formatter={(value) => [Number(value).toLocaleString() + " 手", "成交量"]}
          contentStyle={{ background: "var(--popover)", borderColor: "var(--border)", color: "var(--foreground)", fontSize: 12 }}
          cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
        <Bar dataKey="volume" isAnimationActive={false}>
          {data.map((point, index) => <Cell key={index} fill={isUp(point, index) ? "var(--up)" : "var(--down)"} opacity={0.75} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  </div>;
}
