import { useEffect, useRef, useState } from "react";
import { TitleBar } from "./components/TitleBar";
import { Sidebar, type PageId } from "./components/Sidebar";
import { ChatDesk } from "./components/pages/ChatDesk";
import { ResearchHub, type PendingResearch } from "./components/pages/ResearchHub";
import { MarketView } from "./components/pages/MarketView";
import { ReportsLibrary } from "./components/pages/ReportsLibrary";
import { SettingsPage, START_PAGE_KEY } from "./components/pages/SettingsPage";
import { api, type AppSettings } from "./api/client";
import type { TemplateId } from "./data/report";

interface ActiveModel { providerId: string; modelId: string; provider: string; model: string; accent: string; }

function isValidActiveModel(p: any): p is ActiveModel {
  // v1.6.0（审计 C4）：后端 payload 形状校验 —— providers 清单变更后老存量记录
  // 会带非法 providerId 进来，Settings 渲染 undefined 会白屏
  return !!p && typeof p.providerId === "string" && typeof p.modelId === "string";
}

export default function App() {
  // v1.8.2：深度研究独立成页并默认作为首屏入口，让用户开箱一眼可见
  const [page, setPage] = useState<PageId>(() => {
    try {
      const p = localStorage.getItem(START_PAGE_KEY);
      return p === "research" || p === "chat" || p === "market" || p === "reports" ? p : "research";
    } catch { return "research"; }
  });
  // 行情页当前标的 + 行情页「深度研究」直接交给独立「深度研究」页执行
  const [marketCode, setMarketCode] = useState("600519.SH");
  const [pendingResearch, setPendingResearch] = useState<PendingResearch | null>(null);
  const [chatDraft, setChatDraft] = useState<{ text: string; nonce: number } | null>(null);
  const [runningResearchCount, setRunningResearchCount] = useState(0);
  const [defaultTemplate, setDefaultTemplate] = useState<TemplateId>("equity_deep");
  useEffect(() => {
    api.appSettings().then((s) => setDefaultTemplate(s.default_template)).catch(() => {});
    const onChange = (e: Event) => {
      const s = (e as CustomEvent<AppSettings>).detail;
      if (s?.default_template) setDefaultTemplate(s.default_template);
    };
    window.addEventListener("finresearch:app-settings", onChange);
    return () => window.removeEventListener("finresearch:app-settings", onChange);
  }, []);
  const [active, setActive] = useState<ActiveModel | null>(() => {
    try {
      const p = JSON.parse(localStorage.getItem("finresearch_active") || "null");
      return isValidActiveModel(p) ? p : null;
    } catch { return null; }
  });
  const [reportsTick, setReportsTick] = useState(0); // 新报告生成后触发报告库刷新
  const [newResearchNonce, setNewResearchNonce] = useState(0);
  // v1.6.0（审计 C4）：先写后读竞态守卫 —— GET 未返回前不触发保存，
  // 否则 localStorage 为空但后端已存当前模型时，null 会把后端值永久覆盖
  // （Settings 的 configsLoadedRef 同款模式，这里此前漏防）。
  const activeLoadedRef = useRef(false);
  // 核验修复：GET 返回前用户已改选模型时，不让迟到的 GET 覆盖用户操作
  // v1.6.3（C4）：ModelPicker 直接选模型（不经 App onActivate）也要置位并同步 active
  const userTouchedRef = useRef(false);
  useEffect(() => {
    const onTouch = (e: Event) => {
      userTouchedRef.current = true;
      const detail = (e as CustomEvent<ActiveModel>).detail;
      if (isValidActiveModel(detail)) setActive(detail);
    };
    window.addEventListener("finresearch:model-changed", onTouch);
    return () => window.removeEventListener("finresearch:model-changed", onTouch);
  }, []);

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

  const activeModel = active ? { provider: active.provider, model: active.modelId || active.model, modelId: active.modelId, accent: active.accent } : null;
  const vis = (id: PageId) => (page === id ? "h-full" : "h-full hidden");

  return (
    <div className="size-full flex flex-col bg-background text-foreground overflow-hidden">
      <TitleBar />
      <div className="flex-1 flex min-h-0">
        <Sidebar page={page} onNavigate={setPage} runningResearchCount={runningResearchCount} />
        <main className="flex-1 min-w-0 relative">
          <div className={vis("research")}>
            <ResearchHub
              activeModel={activeModel}
              visible={page === "research"}
              defaultTemplate={defaultTemplate}
              pendingResearch={pendingResearch}
              newTaskNonce={newResearchNonce}
              onGoSettings={() => setPage("settings")}
              onOpenReports={() => setPage("reports")}
              onAskInChat={(text) => {
                setChatDraft({ text, nonce: Date.now() });
                setPage("chat");
              }}
              onReportGenerated={() => setReportsTick((t) => t + 1)}
              onRunningCountChange={setRunningResearchCount}
            />
          </div>
          <div className={vis("chat")}>
            <ChatDesk
              activeModel={activeModel}
              onGoSettings={() => setPage("settings")}
              onGoResearch={() => setPage("research")}
              draftPrompt={chatDraft}
              onReportGenerated={() => setReportsTick((t) => t + 1)}
            />
          </div>
          <div className={vis("market")}>
            <MarketView
              code={marketCode}
              visible={page === "market"}
              onSelect={setMarketCode}
              onResearch={(code, name) => {
                setPendingResearch({ code, name, nonce: Date.now(), autoStart: true });
                setPage("research");
              }}
            />
          </div>
          <div className={vis("reports")}>
            <ReportsLibrary
              tick={reportsTick}
              visible={page === "reports"}
              onNew={() => {
                setNewResearchNonce((n) => n + 1);
                setPage("research");
              }}
            />
          </div>
          <div className={vis("settings")}>
            <SettingsPage
              active={active ? { providerId: active.providerId, modelId: active.modelId } : null}
              onActivate={(p) => {
                userTouchedRef.current = true;
                try {
                  localStorage.setItem("finresearch_active", JSON.stringify(p));
                } catch {}
                setActive(p);
                window.dispatchEvent(new CustomEvent("finresearch:model-changed", { detail: p }));
              }}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
