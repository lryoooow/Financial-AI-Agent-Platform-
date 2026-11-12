import { api } from "../api/client";
import type { TemplateId } from "../data/report";

/** 常见指数代码白名单（防止 000001.SH 上证指数 / 399001 深证成指被静默当成个股代码）。 */
const KNOWN_INDEX_FULL = new Set([
  "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000852.SH", "000905.SH",
  "399001.SZ", "399006.SZ", "399300.SZ", "899050.BJ",
]);

export function isIndexCodeInput(input: string): boolean {
  const raw = input.trim().toUpperCase();
  if (KNOWN_INDEX_FULL.has(raw)) return true;
  const m = raw.match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
  if (!m) return false;
  const pure = m[1];
  const ex = m[2];
  if (ex && KNOWN_INDEX_FULL.has(`${pure}.${ex}`)) return true;
  if (!ex && /^(399\d{3}|899\d{3})$/.test(pure)) return true;
  return false;
}

/** 6 位代码（可带 .SH/.SZ/.BJ）规范化为带市场后缀的代码；不是个股/ETF 代码返回 null。 */
export function normalizeCode(input: string): string | null {
  if (isIndexCodeInput(input)) return null;
  const raw = input.trim().toUpperCase();
  const m = raw.match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
  if (!m) return null;
  const pure = m[1];
  if (m[2]) return `${pure}.${m[2]}`;
  if (/^(60|68|90|11|50|51|52|56|58)/.test(pure)) return `${pure}.SH`;
  if (/^(43|83|87|88|92)/.test(pure)) return `${pure}.BJ`; // 北交所 920xxx
  return `${pure}.SZ`;
}

/** 研究目标解析：行业模板输入即行业名；个股模板先认代码，再按名称/拼音查全 A 股。 */
export async function resolveResearchTarget(
  q: string,
  template: TemplateId,
): Promise<{ code: string; name: string } | null> {
  const text = q.trim();
  if (!text) return null;
  if (template === "industry") return { code: text, name: text };
  if (isIndexCodeInput(text)) return null;
  const codeLike = normalizeCode(text);
  if (codeLike) {
    // 代码输入也尽量补出名称（卡片/对话标题更易读）；查名失败不影响研究
    try {
      const hits = await api.resolve(codeLike.slice(0, 6), 1);
      const hit = hits.find((h) => h.code === codeLike);
      if (hit) return { code: codeLike, name: hit.name };
    } catch { /* ignore */ }
    return { code: codeLike, name: codeLike };
  }
  const hits = await api.resolve(text, 1);
  return hits.length ? { code: hits[0].code, name: hits[0].name } : null;
}
