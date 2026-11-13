import { useState } from "react";
import { Check, ChevronDown, Database, Loader2, X } from "lucide-react";
import type { ToolArtifact, ToolInvocation } from "../../api/client";

export function ChatToolCalls({ calls, artifacts, onOpen }: {
  calls: ToolInvocation[]; artifacts: ToolArtifact[]; onOpen: (artifact: ToolArtifact) => void;
}) {
  const [open, setOpen] = useState(false);
  const pending = calls.filter((call) => call.status === "start");
  return (
    <div className="mb-2">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex h-6 w-full items-center gap-2 rounded px-1 text-left text-xs text-muted-foreground hover:bg-accent">
        {pending.length ? <Loader2 className="size-3.5 animate-spin text-gold" /> : <Database className="size-3.5" />}
        <span className="flex-1 truncate">{pending[0]?.label || "工具调用"}</span>
        <span className="font-mono tabular">{calls.length}</span>
        <ChevronDown className={"size-3.5 transition-transform " + (open ? "rotate-180" : "")} />
      </button>
      <div className="workbench-disclosure grid transition-[grid-template-rows] duration-150" style={{ gridTemplateRows: open ? "1fr" : "0fr" }}>
        <div className="min-h-0 overflow-hidden">
          <ul className="max-h-64 space-y-1 overflow-auto border-l pl-3 pt-1">
            {calls.map((call, index) => {
              const artifact = artifacts.find((entry) => entry.call_id === call.call_id);
              return (
                <li key={call.call_id || call.name + index} className="rounded bg-background/60 p-2 text-xs">
                  <div className="flex items-center gap-2">
                    {call.status === "start" ? <Loader2 className="size-3 animate-spin text-gold" /> :
                      call.status === "ok" ? <Check className="size-3 text-down" /> : <X className="size-3 text-up" />}
                    <span className="flex-1">{call.label || call.name}</span>
                    {call.elapsed_ms != null && <span className="font-mono text-muted-foreground">{call.elapsed_ms} ms</span>}
                    {artifact && <button type="button" className="text-gold underline" onClick={() => onOpen(artifact)}>查看数据</button>}
                  </div>
                  {call.args_summary && call.args_summary !== "{}" && <p className="break-all font-mono text-muted-foreground">{call.args_summary}</p>}
                  {call.provenance?.source && <p className="text-muted-foreground">{call.provenance.source} {call.provenance.fetched_at}</p>}
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}
