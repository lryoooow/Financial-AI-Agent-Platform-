import { useState } from "react";
import { ResearchTasks } from "./ResearchTasks";
import type { ReportItem } from "./ReportsLibrary";

interface ActiveModel { provider: string; model: string; accent: string; }

interface Props {
  initialTrigger?: string;
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onReportGenerated: (r: ReportItem) => void;
}

export function ResearchStation({ initialTrigger, activeModel, onGoSettings, onReportGenerated }: Props) {
  const [code, setCode] = useState(() => initialTrigger?.split(":")[0] || "600519.SH");

  return (
    <ResearchTasks
      selectedCode={code}
      onSelectCode={setCode}
      activeModel={activeModel}
      onGoSettings={onGoSettings}
      onReportGenerated={onReportGenerated}
    />
  );
}
