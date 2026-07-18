import { motion } from "motion/react";
import { LayoutDashboard, FileText, MessageSquare, Settings, ShieldCheck } from "lucide-react";

export type PageId = "chat" | "research" | "reports" | "settings";

const NAV: { id: PageId; label: string; sub: string; icon: typeof LayoutDashboard }[] = [
  { id: "chat", label: "专业对话", sub: "Chat", icon: MessageSquare },
  { id: "research", label: "研究任务", sub: "Research", icon: LayoutDashboard },
  { id: "reports", label: "报告库", sub: "Reports", icon: FileText },
  { id: "settings", label: "模型配置", sub: "Providers", icon: Settings },
];

export function Sidebar({ page, onNavigate }: { page: PageId; onNavigate: (p: PageId) => void }) {
  return (
    <aside className="w-[212px] shrink-0 flex flex-col bg-sidebar border-r border-sidebar-border">
      <nav className="flex-1 p-3 space-y-1">
        <p className="px-3 pt-2 pb-3 text-[10px] tracking-[0.2em] text-muted-foreground/70 uppercase">
          导航
        </p>
        {NAV.map((item, i) => {
          const active = page === item.id;
          const Icon = item.icon;
          return (
            <motion.button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: 0.05 * i, duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-full flex items-center gap-3 px-3 py-2.5 rounded-lg group"
            >
              {active && (
                <motion.div
                  layoutId="nav-active"
                  className="absolute inset-0 rounded-lg bg-sidebar-accent border border-sidebar-border"
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
                className={`relative size-[18px] transition-colors ${
                  active ? "text-gold" : "text-muted-foreground group-hover:text-foreground"
                }`}
                strokeWidth={active ? 2.4 : 2}
              />
              <div className="relative flex flex-col items-start leading-tight">
                <span className={`text-sm transition-colors ${active ? "text-foreground" : "text-sidebar-foreground group-hover:text-foreground"}`}>
                  {item.label}
                </span>
                <span className="text-[10px] tracking-wide text-muted-foreground/60">{item.sub}</span>
              </div>
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
