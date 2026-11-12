import { motion } from "motion/react";
import { CandlestickChart, FileText, FlaskConical, Loader2, MessageSquare, Settings, ShieldCheck } from "lucide-react";

export type PageId = "research" | "chat" | "market" | "reports" | "settings";

const NAV: { id: PageId; label: string; sub: string; icon: typeof MessageSquare; featured?: boolean }[] = [
  { id: "research", label: "深度研究", sub: "AI 研报生成 · 任务台", icon: FlaskConical, featured: true },
  { id: "chat", label: "投研问答", sub: "多轮问股 · 看图解盘", icon: MessageSquare },
  { id: "market", label: "实时行情", sub: "分时 K 线 · 五档盘口", icon: CandlestickChart },
  { id: "reports", label: "报告库", sub: "历史研报 · 知识检索", icon: FileText },
  { id: "settings", label: "设置", sub: "模型密钥 · 偏好配置", icon: Settings },
];

export function Sidebar({
  page,
  onNavigate,
  runningResearchCount = 0,
}: {
  page: PageId;
  onNavigate: (p: PageId) => void;
  runningResearchCount?: number;
}) {
  return (
    <aside className="w-[216px] shrink-0 flex flex-col bg-sidebar border-r border-sidebar-border">
      <nav className="flex-1 p-3 space-y-1.5">
        <p className="px-3 pt-2 pb-2 text-[10px] tracking-[0.2em] text-muted-foreground/70 uppercase">
          核心工作台
        </p>
        {NAV.map((item, i) => {
          const active = page === item.id;
          const Icon = item.icon;
          const isFeatured = Boolean(item.featured);
          return (
            <motion.button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: 0.04 * i, duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
              className={`relative w-full flex items-center gap-3 px-3 py-2.5 rounded-lg group transition-colors ${
                !active && isFeatured ? "border border-gold/25 bg-gold/[0.05] hover:bg-gold/[0.09]" : ""
              }`}
            >
              {active && (
                <motion.div
                  layoutId="nav-active"
                  className={`absolute inset-0 rounded-lg border ${
                    isFeatured ? "bg-gold/[0.12] border-gold/40" : "bg-sidebar-accent border-sidebar-border"
                  }`}
                  transition={{ type: "spring", stiffness: 400, damping: 32 }}
                />
              )}
              {active && (
                <motion.div
                  layoutId="nav-bar"
                  className="absolute left-0 top-1/2 -translate-y-1/2 h-6 w-[3px] rounded-full bg-gold"
                  transition={{ type: "spring", stiffness: 400, damping: 32 }}
                />
              )}
              <Icon
                className={`relative size-[18px] shrink-0 transition-colors ${
                  active || isFeatured ? "text-gold" : "text-muted-foreground group-hover:text-foreground"
                }`}
                strokeWidth={active ? 2.4 : 2}
              />
              <div className="relative min-w-0 flex-1 flex flex-col items-start leading-tight text-left">
                <span className={`text-sm font-medium transition-colors ${active ? "text-foreground" : isFeatured ? "text-foreground/95" : "text-sidebar-foreground group-hover:text-foreground"}`}>
                  {item.label}
                </span>
                <span className="text-[10.5px] tracking-wide text-muted-foreground/75 truncate w-full mt-0.5">{item.sub}</span>
              </div>
              {isFeatured && (
                runningResearchCount > 0 ? (
                  <span className="relative ml-auto flex items-center gap-1 rounded-full bg-gold/20 border border-gold/45 px-1.5 py-0.5 text-[10px] font-medium text-gold shrink-0" title={`当前有 ${runningResearchCount} 个深度研究任务正在运行`}>
                    <Loader2 className="size-2.5 animate-spin" />
                    {runningResearchCount}
                  </span>
                ) : (
                  <span className="relative ml-auto rounded-full bg-gold/15 border border-gold/35 px-1.5 py-0.5 text-[10px] font-medium text-gold shrink-0">
                    核心
                  </span>
                )
              )}
            </motion.button>
          );
        })}
      </nav>

      <div className="p-3">
        <div className="rounded-lg border border-sidebar-border bg-sidebar-accent/40 p-3">
          <div className="flex items-center gap-2 text-xs text-foreground/80">
            <ShieldCheck className="size-4 text-down" />
            <span>只读 · 隐私优先</span>
          </div>
          <p className="mt-1.5 text-[10.5px] leading-relaxed text-muted-foreground">
            数据本地留存，永不接入交易。AI 仅供研究参考。
          </p>
        </div>
      </div>
    </aside>
  );
}
