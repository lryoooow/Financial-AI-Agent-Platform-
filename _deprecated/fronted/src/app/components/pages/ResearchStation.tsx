import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { MarketView } from "./MarketView";
import { Workspace } from "./Workspace";
import type { ReportItem } from "./ReportsLibrary";

interface ActiveModel { provider: string; model: string; accent: string; }

interface Props {
  initialTrigger?: string;            // 跨页触发（独立"行情走势"页点"生成研究报告"跳来时带 code）
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onReportGenerated: (r: ReportItem) => void;
}

/**
 * 统一研究台（req3）：行情走势 + 研究工作台 绑定在同一页。
 * 纵向分栏 —— 上：行情（图 + 报价 + 盘口 + 指标）；下：AI 研究（管线 + 报告）。
 * 共享 selectedCode：图上"分析当前标的"一键带到下方法线，问答切标的→上图同步。
 * 复用现有 MarketView / Workspace 组件，不改动它们。
 */
export function ResearchStation({ initialTrigger, activeModel, onGoSettings, onReportGenerated }: Props) {
  const [code, setCode] = useState("600519.SH");
  const [trigger, setTrigger] = useState<string | undefined>();

  // 跨页触发：把外部 initialTrigger 同步成本页 code + 触发 Workspace
  useEffect(() => {
    if (initialTrigger) {
      setCode(initialTrigger.split(":")[0]);
      setTrigger(initialTrigger);
    }
  }, [initialTrigger]);

  // 图上"分析当前标的" → 下方法线带同一 code 跑研究（不跳页）
  const analyze = (c: string) => {
    setCode(c);
    setTrigger(c + ":" + Date.now());
  };

  return (
    <div className="h-full min-h-0 overflow-hidden p-3">
      <PanelGroup direction="vertical" className="h-full">
        {/* 上：行情走势，绑定 code */}
        <Panel defaultSize={55} minSize={30}>
          <div className="h-full min-h-0 overflow-y-auto">
            <MarketView code={code} onSelect={setCode} onResearch={analyze} />
          </div>
        </Panel>

        <PanelResizeHandle className="h-1.5 bg-border hover:bg-gold/50 data-[resize-handle-state=drag]:bg-gold transition-colors" />

        {/* 下：AI 研究工作台，同一 code */}
        <Panel defaultSize={45} minSize={22}>
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
  );
}
