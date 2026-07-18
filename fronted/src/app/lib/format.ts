export const fmt = (n: number, d = 2) =>
  n.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });

export const signed = (n: number, d = 2) => (n >= 0 ? "+" : "") + fmt(n, d);

// 涨跌 → 颜色类（A股：红涨绿跌）
export const trendClass = (n: number) =>
  n > 0 ? "text-up" : n < 0 ? "text-down" : "text-muted-foreground";

export const trendVar = (n: number) =>
  n > 0 ? "var(--up)" : n < 0 ? "var(--down)" : "var(--muted-foreground)";
