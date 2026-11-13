import { useEffect, useState } from "react";
import { Bell, CandlestickChart, FileText, FlaskConical, Gauge, MessageSquare, PanelLeftClose, PanelLeftOpen, Settings } from "lucide-react";
import { api } from "../api/client";

export type PageId = "overview" | "research" | "chat" | "market" | "reports" | "settings";

const NAV = [
  { id: "overview", label: "市场总览", icon: Gauge },
  { id: "market", label: "看盘与自选", icon: CandlestickChart },
  { id: "research", label: "深度研究", icon: FlaskConical },
  { id: "chat", label: "投研问答", icon: MessageSquare },
  { id: "reports", label: "报告与知识库", icon: FileText },
  { id: "settings", label: "设置", icon: Settings },
] as const;

export function Sidebar({ page, onNavigate, runningResearchCount = 0, collapsed, onToggle }: {
  page: PageId; onNavigate: (page: PageId) => void; runningResearchCount?: number;
  collapsed: boolean; onToggle: () => void;
}) {
  const [scheduled, setScheduled] = useState(false);
  useEffect(() => {
    if (page !== "settings") api.schedulerConfig().then((result) => setScheduled(result.enabled)).catch(() => {});
  }, [page]);
  return (
    <aside aria-label="工作台导航" className={"flex shrink-0 flex-col border-r bg-sidebar transition-[width] duration-150 " + (collapsed ? "w-[52px]" : "w-[180px]")}>
      <nav className="flex flex-1 flex-col gap-1 p-2">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button type="button" key={id} title={label} aria-label={label} aria-current={page === id ? "page" : undefined}
            onClick={() => onNavigate(id)}
            className={"relative flex h-9 items-center gap-2 rounded-md px-2.5 text-left transition-colors " +
              (page === id ? "bg-[var(--gold-muted)] text-gold" : "text-sidebar-foreground hover:bg-sidebar-accent") +
              (id === "settings" ? " mt-auto" : "")}>
            {page === id && <span aria-hidden className="absolute left-0 h-5 w-[2px] rounded-r bg-action" />}
            <Icon className="size-4 shrink-0" />
            {!collapsed && <span className="flex-1 truncate">{label}</span>}
            {id === "research" && runningResearchCount > 0 && <span className={(collapsed ? "absolute right-0 top-0 " : "") + "rounded bg-[var(--gold-muted)] px-1 text-xs text-gold tabular"}>{runningResearchCount}</span>}
          </button>
        ))}
      </nav>
      <div className="flex items-center gap-2 px-4 pb-2 text-xs text-muted-foreground" title={scheduled ? "收盘巡检已启用" : "收盘巡检已停用"}>
        <Bell className={"size-3.5 shrink-0 " + (scheduled ? "text-gold" : "")} />
        {!collapsed && (scheduled ? "收盘巡检已启用" : "收盘巡检已停用")}
      </div>
      <button type="button" className="flex h-10 items-center gap-2 border-t px-4 text-muted-foreground hover:bg-sidebar-accent" title="折叠侧栏 (Ctrl+B)" onClick={onToggle} aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}>
        {collapsed ? <PanelLeftOpen className="size-4" /> : <><PanelLeftClose className="size-4" /> 折叠侧栏</>}
      </button>
    </aside>
  );
}
