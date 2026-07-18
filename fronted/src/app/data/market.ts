// A 股常用标的快捷入口。这里仅保存名称/代码等元数据，行情数值必须来自后端真实接口。

export interface Stock {
  code: string;
  name: string;
  exchange: string;
  price: number;
  change: number;
  changePct: number;
  open: number;
  high: number;
  low: number;
  prevClose: number;
  volume: string;
  turnover: string;
  marketCap: string;
  pe: number;
  pb: number;
  roe: number;
  industry: string;
}

const stockMeta = (code: string, name: string, exchange: string, industry: string): Stock => ({
  code,
  name,
  exchange,
  industry,
  price: 0,
  change: 0,
  changePct: 0,
  open: 0,
  high: 0,
  low: 0,
  prevClose: 0,
  volume: "—",
  turnover: "—",
  marketCap: "—",
  pe: 0,
  pb: 0,
  roe: 0,
});

export const WATCHLIST: Stock[] = [
  stockMeta("600519.SH", "贵州茅台", "上证", "白酒"),
  stockMeta("300750.SZ", "宁德时代", "深证", "电池"),
  stockMeta("601318.SH", "中国平安", "上证", "保险"),
  stockMeta("000858.SZ", "五粮液", "深证", "白酒"),
  stockMeta("002594.SZ", "比亚迪", "深证", "汽车"),
  stockMeta("600036.SH", "招商银行", "上证", "银行"),
];

export interface Candle {
  date: string;
  open: number;
  close: number;
  high: number;
  low: number;
  volume: number;
  ma5: number;
  ma10: number;
  ma20: number;
}
