import { useState } from "react";
import { Brain, ChevronDown } from "lucide-react";

export function ChatReasoning({ text, elapsedMs, live }: { text: string; elapsedMs: number; live: boolean }) {
  const [open, setOpen] = useState(false);
  const seconds = Math.max(0, Math.round(elapsedMs / 1000));
  return (
    <div className="mb-2">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex h-6 w-full items-center gap-2 rounded px-1 text-left text-xs text-muted-foreground hover:bg-accent">
        <Brain className={"size-3.5 " + (live ? "animate-pulse text-gold" : "")} />
        <span className="flex-1">{live ? "正在思考" : "已思考"} {seconds} 秒</span>
        <ChevronDown className={"size-3.5 transition-transform " + (open ? "rotate-180" : "")} />
      </button>
      <div className="workbench-disclosure grid transition-[grid-template-rows] duration-150" style={{ gridTemplateRows: open ? "1fr" : "0fr" }}>
        <div className="min-h-0 overflow-hidden">
          <p className="max-h-48 overflow-auto whitespace-pre-wrap border-l pl-3 py-1 text-xs text-muted-foreground">{text}</p>
        </div>
      </div>
    </div>
  );
}
