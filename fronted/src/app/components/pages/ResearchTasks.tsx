import { useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { LineChart } from "lucide-react";
import { MarketView } from "./MarketView";
import { Workspace } from "./Workspace";
import type { ReportItem } from "./ReportsLibrary";

interface ActiveModel { provider: string; model: string; accent: string; }

interface Props {
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onReportGenerated: (r: ReportItem) => void;
}

/**
 * 研究任务页：独立的深度研究入口。
 * 上：辅助行情组件 MarketView（可折叠，按需复用）；下：研究工作台 Workspace。
 * 共享 selectedCode：行情图上"生成研究报告"一键带到下方法线跑研究。
 *
 * v1.6.0（审计 C2）：结构收敛为单一父级 —— 此前 showMarket 两分支的父节点类型不同
 * （PanelGroup vs div），切换「隐藏行情/行情辅助」会整树重挂载：Workspace 里的研究
 * SSE 连接泄漏、运行中任务从 UI 消失。现在 Workspace 始终挂在同一个 PanelGroup 下，
 * 折叠只卸载 MarketView 子树。
 */
export function ResearchTasks({ activeModel, onGoSettings, onReportGenerated }: Props) {
  const [code, setCode] = useState("600519.SH");
  const [trigger, setTrigger] = useState<string | undefined>();
  const [showMarket, setShowMarket] = useState(true);

  // 行情图上"生成研究报告" → 下方法线带同一 code 跑研究
  const analyze = (c: string) => {
    setCode(c);
    setTrigger(c + ":" + Date.now());
  };

  return (
    <div className="h-full min-h-0 flex flex-col overflow-hidden">
      <div className="flex items-center justify-between px-4 pt-3 pb-1">
        <h1 className="font-display text-base font-semibold">研究任务</h1>
        <button
          onClick={() => setShowMarket((v) => !v)}
          className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md bg-secondary hover:bg-secondary/70 transition-colors"
          title="按需显示/隐藏辅助行情"
        >
          <LineChart className="size-3.5" />
          {showMarket ? "隐藏行情" : "行情辅助"}
        </button>
      </div>
      <div className="flex-1 min-h-0 p-3 pt-1">
        <PanelGroup direction="vertical" className="h-full">
          {showMarket && (
            <>
              <Panel defaultSize={46} minSize={22}>
                <div className="h-full min-h-0 overflow-y-auto">
                  <MarketView code={code} onSelect={setCode} onResearch={analyze} />
                </div>
              </Panel>
              <PanelResizeHandle className="h-1.5 bg-border hover:bg-gold/50 data-[resize-handle-state=drag]:bg-gold transition-colors" />
            </>
          )}
          <Panel defaultSize={showMarket ? 54 : 100} minSize={25}>
            <div className="h-full min-h-0 overflow-y-auto">
              <Workspace
                key={trigger ?? "idle"}
                initialCode={trigger}
                selectedCode={code}
                activeModel={activeModel}
                onGoSettings={onGoSettings}
                onReportGenerated={onReportGenerated}
              />
            </div>
          </Panel>
        </PanelGroup>
      </div>
    </div>
  );
}
