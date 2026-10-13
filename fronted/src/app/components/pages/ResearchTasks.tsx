import { useEffect, useState } from "react";
import { FileText, Loader2, PlayCircle } from "lucide-react";
import { api, type ResearchTaskSummary } from "../../api/client";
import type { TemplateId } from "../../data/report";
import { Workspace, type AttachRequest } from "./Workspace";
import type { ReportItem } from "./ReportsLibrary";

interface ActiveModel { provider: string; model: string; accent: string; }

/** 任务状态徽标：运行中/排队中亮金，完成绿，失败红。 */
function StatusBadge({ status }: { status: ResearchTaskSummary["status"] }) {
  const map: Record<string, { label: string; cls: string }> = {
    running: { label: "运行中", cls: "text-gold bg-gold/10 border-gold/30" },
    pending: { label: "排队中", cls: "text-gold bg-gold/10 border-gold/30" },
    done: { label: "已完成", cls: "text-down bg-down/10 border-down/30" },
    succeeded: { label: "已完成", cls: "text-down bg-down/10 border-down/30" },
    error: { label: "失败", cls: "text-up bg-up/10 border-up/30" },
  };
  const s = map[status] ?? { label: status, cls: "text-muted-foreground bg-secondary/50 border-border" };
  return <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] leading-none ${s.cls}`}>{s.label}</span>;
}

/** 最近研究任务条：断线/刷新后的恢复入口。
 *
 * 后端 GET /api/research/tasks 一直存在却从未被前端消费——运行中的任务对用户不可见，
 * "报告库自己冒出报告"与工作区报错的矛盾状态即源于此。运行中/排队中的任务提供
 * 「恢复查看」（直连其 SSE，回放 + 续播）；已完成/失败的仅作状态可见。 */
function RecentTasksStrip({ onAttach }: { onAttach: (t: ResearchTaskSummary) => void }) {
  const [tasks, setTasks] = useState<ResearchTaskSummary[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const refresh = () =>
      api.researchTasks()
        .then((r) => { if (!cancelled) { setTasks(r.tasks.slice(0, 8)); setLoaded(true); } })
        .catch(() => { if (!cancelled) setLoaded(true); });
    void refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  if (!loaded || tasks.length === 0) return null;
  return (
    <div className="mx-auto mt-5 mb-0 max-w-[min(100%-2.5rem,1600px)] rounded-xl border border-border bg-card px-3.5 py-2.5">
      <div className="mb-2 text-[11px] text-muted-foreground">最近研究任务</div>
      <div className="flex gap-2 overflow-x-auto pb-0.5">
        {tasks.map((t) => (
          <div
            key={t.id}
            className="flex shrink-0 items-center gap-2 rounded-lg border border-border bg-secondary/30 px-2.5 py-1.5 text-xs"
            title={t.error ? `失败原因：${t.error}` : `${t.code} · ${t.template}`}
          >
            <StatusBadge status={t.status} />
            <span className="font-mono text-foreground/85">{t.code}</span>
            {(t.status === "running" || t.status === "pending") && (
              <button
                onClick={() => onAttach(t)}
                className="flex items-center gap-1 rounded-md bg-gold/15 px-1.5 py-0.5 text-[11px] text-gold transition-colors hover:bg-gold/25"
              >
                {t.status === "running" ? <PlayCircle className="size-3" /> : <Loader2 className="size-3 animate-spin" />}
                恢复查看
              </button>
            )}
            {t.status === "succeeded" && t.report_id && (
              <button
                onClick={() => onAttach(t)}
                className="flex items-center gap-1 rounded-md bg-down/10 px-1.5 py-0.5 text-[11px] text-down transition-colors hover:bg-down/20"
              >
                <FileText className="size-3" />
                查看报告
              </button>
            )}
            {t.status === "error" && t.error && (
              <span className="max-w-40 truncate text-[10px] text-muted-foreground" title={t.error}>{t.error}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export function ResearchTasks({
  activeModel,
  selectedCode,
  onSelectCode,
  onGoSettings,
  onReportGenerated,
}: {
  activeModel: ActiveModel | null;
  selectedCode: string;
  onSelectCode: (code: string) => void;
  onGoSettings: () => void;
  onReportGenerated: (report: ReportItem) => void;
}) {
  const [attach, setAttach] = useState<AttachRequest | undefined>();

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <RecentTasksStrip
        onAttach={(t) =>
          setAttach({ taskId: t.id, code: t.code, template: t.template as TemplateId, nonce: Date.now() })
        }
      />
      <Workspace
        selectedCode={selectedCode}
        onSelectCode={onSelectCode}
        activeModel={activeModel}
        onGoSettings={onGoSettings}
        onReportGenerated={onReportGenerated}
        attachRequest={attach}
      />
    </div>
  );
}
