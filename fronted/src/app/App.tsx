import { useEffect, useState } from "react";
import { TitleBar } from "./components/TitleBar";
import { Sidebar, type PageId } from "./components/Sidebar";
import { ChatDesk } from "./components/pages/ChatDesk";
import { ResearchTasks } from "./components/pages/ResearchTasks";
import { ReportsLibrary } from "./components/pages/ReportsLibrary";
import { Settings } from "./components/pages/Settings";
import { KnowledgeBase } from "./components/pages/KnowledgeBase";
import { MarketView } from "./components/pages/MarketView";
import { api } from "./api/client";

interface ActiveModel { providerId: string; modelId: string; provider: string; model: string; accent: string; }

export default function App() {
  // 1.2.0：默认首页"专业对话"
  const [page, setPage] = useState<PageId>("chat");
  const [active, setActive] = useState<ActiveModel | null>(() => {
    try { return JSON.parse(localStorage.getItem("finresearch_active") || "null"); } catch { return null; }
  });
  const [reportsTick, setReportsTick] = useState(0); // 新报告生成后触发报告库刷新
  const [selectedCode, setSelectedCode] = useState("000001.SH"); // 初始展示大盘（上证指数）而非个股

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

  const renderPage = () => {
    switch (page) {
      case "market":
        return (
          <MarketView
            code={selectedCode}
            active
            activeModel={activeModel}
            onSelect={setSelectedCode}
            onOpenResearch={(code) => {
              setSelectedCode(code);
              setPage("research");
            }}
            onGoSettings={() => setPage("settings")}
          />
        );
      case "research":
        return (
          <ResearchTasks
            selectedCode={selectedCode}
            onSelectCode={setSelectedCode}
            activeModel={activeModel}
            onGoSettings={() => setPage("settings")}
            onReportGenerated={() => setReportsTick((tick) => tick + 1)}
          />
        );
      case "reports":
        return <ReportsLibrary tick={reportsTick} onNew={() => setPage("research")} />;
      case "kb":
        return <KnowledgeBase />;
      case "settings":
        return (
          <Settings
            active={active ? { providerId: active.providerId, modelId: active.modelId } : null}
            onActivate={(provider) => setActive(provider)}
          />
        );
      case "chat":
      default:
        return <ChatDesk activeModel={activeModel} onGoSettings={() => setPage("settings")} />;
    }
  };

  return (
    <div className="size-full flex flex-col bg-background text-foreground overflow-hidden">
      <TitleBar />
      <div className="flex-1 flex min-h-0">
        <Sidebar page={page} onNavigate={setPage} />
        <main className="relative min-w-0 flex-1" data-page={page}>{renderPage()}</main>
      </div>
    </div>
  );
}
