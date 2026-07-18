import { useEffect, useState } from "react";
import { TitleBar } from "./components/TitleBar";
import { Sidebar, type PageId } from "./components/Sidebar";
import { ChatDesk } from "./components/pages/ChatDesk";
import { ResearchTasks } from "./components/pages/ResearchTasks";
import { ReportsLibrary } from "./components/pages/ReportsLibrary";
import { Settings } from "./components/pages/Settings";
import { api } from "./api/client";

interface ActiveModel { providerId: string; modelId: string; provider: string; model: string; accent: string; }

export default function App() {
  // 1.2.0：默认首页"专业对话"
  const [page, setPage] = useState<PageId>("chat");
  const [active, setActive] = useState<ActiveModel | null>(() => {
    try { return JSON.parse(localStorage.getItem("finresearch_active") || "null"); } catch { return null; }
  });
  const [reportsTick, setReportsTick] = useState(0); // 新报告生成后触发报告库刷新

  useEffect(() => {
    let cancelled = false;
    api.localSetting("model-configs")
      .then((r) => { if (!cancelled && r.payload) localStorage.setItem("finresearch_keys", JSON.stringify(r.payload)); })
      .catch(() => {});
    api.localSetting("active-model")
      .then((r) => { if (!cancelled && r.payload) setActive(r.payload as ActiveModel); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    localStorage.setItem("finresearch_active", JSON.stringify(active));
    api.saveLocalSetting("active-model", active).catch(() => {});
  }, [active]);

  const activeModel = active ? { provider: active.provider, model: active.model, accent: active.accent } : null;
  const vis = (id: PageId) => (page === id ? "h-full" : "h-full hidden");

  return (
    <div className="size-full flex flex-col bg-background text-foreground overflow-hidden">
      <TitleBar />
      <div className="flex-1 flex min-h-0">
        <Sidebar page={page} onNavigate={setPage} />
        <main className="flex-1 min-w-0 relative">
          <div className={vis("chat")}>
            <ChatDesk activeModel={activeModel} onGoSettings={() => setPage("settings")} />
          </div>
          <div className={vis("research")}>
            <ResearchTasks
              activeModel={activeModel}
              onGoSettings={() => setPage("settings")}
              onReportGenerated={() => setReportsTick((t) => t + 1)}
            />
          </div>
          <div className={vis("reports")}>
            <ReportsLibrary tick={reportsTick} onNew={() => setPage("research")} />
          </div>
          <div className={vis("settings")}>
            <Settings
              active={active ? { providerId: active.providerId, modelId: active.modelId } : null}
              onActivate={(p) => setActive(p)}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
