/** 标的使用频率学习（localStorage，桌面单机无账号——换机丢失可接受）。
 *
 * 记录 {code: {count, lastUsed, name}}，上限 50 条按 lastUsed 淘汰；
 * 埋点在行情页选中、研究页真正发起任务两处（选中即算一次使用，60s 内同标的去重）。
 * 排序：常用区按 count 降序（并列按 lastUsed），最近使用按 lastUsed 降序。
 */

const KEY = "finresearch_usage_v1";
const CAP = 50;
const DEDUP_MS = 60_000;

export interface UsageEntry { count: number; lastUsed: number; name?: string; }
export type UsageMap = Record<string, UsageEntry>;

function read(): UsageMap {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
    return raw && typeof raw === "object" ? (raw as UsageMap) : {};
  } catch {
    return {};
  }
}

function write(map: UsageMap) {
  const codes = Object.keys(map);
  if (codes.length > CAP) {
    // 超限按 lastUsed 淘汰最久未用的
    codes.sort((a, b) => (map[a].lastUsed || 0) - (map[b].lastUsed || 0));
    for (const c of codes.slice(0, codes.length - CAP)) delete map[c];
  }
  try { localStorage.setItem(KEY, JSON.stringify(map)); } catch { /* 满/禁用则放弃 */ }
}

export function trackUsage(code: string, name?: string): void {
  if (!code) return;
  const map = read();
  const prev = map[code];
  if (prev && Date.now() - prev.lastUsed < DEDUP_MS) {
    // 去重窗口内只刷新名字，不计数（防止轮询/重复合中刷爆 count）
    if (name && !prev.name) { prev.name = name; write(map); }
    return;
  }
  map[code] = { count: (prev?.count || 0) + 1, lastUsed: Date.now(), name: name || prev?.name };
  write(map);
}

/** 常用推荐：按使用次数降序（并列按最近使用），取前 n。 */
export function getFrequent(n = 6): { code: string; name: string }[] {
  const map = read();
  return Object.entries(map)
    .sort((a, b) => (b[1].count - a[1].count) || (b[1].lastUsed - a[1].lastUsed))
    .slice(0, n)
    .map(([code, e]) => ({ code, name: e.name || code }));
}

/** 最近使用：按时间降序，取前 n。 */
export function getRecent(n = 5): { code: string; name: string }[] {
  const map = read();
  return Object.entries(map)
    .sort((a, b) => b[1].lastUsed - a[1].lastUsed)
    .slice(0, n)
    .map(([code, e]) => ({ code, name: e.name || code }));
}
