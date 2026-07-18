export type TemplateId = "equity_deep" | "industry" | "earnings" | "risk";

export const TEMPLATES: { id: TemplateId; label: string; desc: string }[] = [
  { id: "equity_deep", label: "个股深度", desc: "公司概况 · 财务分析 · 估值 · 风险" },
  { id: "industry", label: "行业研究", desc: "市场规模 · 竞争格局 · 产业链" },
  { id: "earnings", label: "财报点评", desc: "业绩快评 · 经营拐点 · 预测调整" },
  { id: "risk", label: "风险评估", desc: "市场风险 · 流动性 · 情景分析" },
];

export interface ReportSection { title: string; body: string; }
export interface ReportDoc {
  summary: string;
  catalyst: string;
  financials: { years: string[]; rows: { name: string; values: string[] }[] };
  sections: ReportSection[];
  risks: string[];
  sources: { title: string; url: string; date: string }[];
  disclaimer: string;
  as_of?: string;
}
