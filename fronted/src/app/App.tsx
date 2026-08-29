import { useEffect, useRef, useState } from "react";
import { TitleBar } from "./components/TitleBar";
import { Sidebar, type PageId } from "./components/Sidebar";
import { ChatDesk } from "./components/pages/ChatDesk";
import { ResearchTasks } from "./components/pages/ResearchTasks";
import { ReportsLibrary } from "./components/pages/ReportsLibrary";
import { Settings } from "./components/pages/Settings";
import { api } from "./api/client";

interface ActiveModel { providerId: string; modelId: string; provider: string; model: string; accent: string; }

function isValidActiveModel(p: any): p is ActiveModel {
  // v1.6.0（审计 C4）：后端 payload 形状校验 —— providers 清单变更后老存量记录
  // 会带非法 providerId 进来，Settings 渲染 undefined 会白屏
  return !!p && typeof p.providerId === "string" && typeof p.modelId === "string";
}

export default function App() {
  // 1.2.0：默认首页"专业对话"
  const [page, setPage] = useState<PageId>("chat");
  const [active, setActive] = useState<ActiveModel | null>(() => {
    try {
      const p = JSON.parse(localStorage.getItem("finresearch_active") || "null");
      return isValidActiveModel(p) ? p : null;
    } catch { return null; }
  });
  const [reportsTick, setReportsTick] = useState(0); // 新报告生成后触发报告库刷新
  // v1.6.0（审计 C4）：先写后读竞态守卫 —— GET 未返回前不触发保存，
  // 否则 localStorage 为空但后端已存当前模型时，null 会把后端值永久覆盖
  // （Settings 的 configsLoadedRef 同款模式，这里此前漏防）。
  const activeLoadedRef = useRef(false);
  // 核验修复：GET 返回前用户已改选模型时，不让迟到的 GET 覆盖用户操作
  const userTouchedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    api.localSetting("model-configs")
      .then((r) => { if (!cancelled && r.payload) localStorage.setItem("finresearch_keys", JSON.stringify(r.payload)); })
      .catch(() => {});
    api.localSetting("active-model")
      .then((r) => {
        if (cancelled || !r.payload || userTouchedRef.current) return;
        const p = r.payload as ActiveModel | null;
        if (isValidActiveModel(p)) setActive(p);
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) activeLoadedRef.current = true; });
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    if (!activeLoadedRef.current) return;
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
              onActivate={(p) => { userTouchedRef.current = true; setActive(p); }}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
