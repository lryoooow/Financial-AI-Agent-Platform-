import { Command } from "cmdk";
import { useEffect, useState } from "react";
import { CandlestickChart, FileSearch, FlaskConical, Search } from "lucide-react";
import { api, type ResolveHit } from "../api/client";
import { isImeKeyEvent } from "../lib/ime";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";

export function CommandPalette({
  open, onOpenChange, onQuote, onResearch, onKnowledge,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onQuote: (hit: ResolveHit) => void;
  onResearch: (hit: ResolveHit) => void;
  onKnowledge: (query: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<ResolveHit[]>([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!open || !query.trim()) { setHits([]); setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(() => {
      api.resolve(query.trim(), 8, controller.signal)
        .then(setHits).catch(() => { if (!controller.signal.aborted) setHits([]); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 170);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [open, query]);
  const run = (action: () => void) => { action(); onOpenChange(false); setQuery(""); };
  const itemStyle = "flex cursor-pointer items-center gap-2 rounded px-3 py-2 data-[selected=true]:bg-accent";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(580px,calc(100%-32px))] max-w-none gap-0 overflow-hidden bg-popover p-0 shadow-[var(--shadow-popover)]">
        <DialogTitle className="sr-only">搜索股票与指令</DialogTitle>
        <DialogDescription className="sr-only">输入股票代码、简称、拼音或知识库关键词</DialogDescription>
        <Command shouldFilter={false} onKeyDownCapture={(event) => {
          if (isImeKeyEvent(event.nativeEvent)) event.stopPropagation();
        }}>
          <div className="flex h-12 items-center gap-2 border-b px-4 pr-12">
            <Search className="size-4 text-muted-foreground" />
            <Command.Input value={query} onValueChange={setQuery} placeholder="代码、简称、拼音或知识库关键词" className="min-w-0 flex-1 outline-none" />
          </div>
          <Command.List className="max-h-[420px] overflow-auto p-2 text-sm">
            {loading && <div role="status" className="px-3 py-2 text-muted-foreground">正在搜索标的…</div>}
            <Command.Item value="knowledge" className={itemStyle} onSelect={() => run(() => onKnowledge(query.trim()))}>
              <FileSearch className="size-4 text-gold" />
              <span className="truncate">{query.trim() ? `搜索报告与知识库：「${query.trim()}」` : "打开报告与知识库"}</span>
            </Command.Item>
            {hits.map((hit) => (
              <div key={hit.code} className="grid grid-cols-[1fr_auto] gap-1 border-t">
                <Command.Item value={hit.code} className={itemStyle} onSelect={() => run(() => onQuote(hit))}>
                  <CandlestickChart className="size-4 text-muted-foreground" />{hit.name}
                  <span className="ml-auto font-mono text-xs text-muted-foreground">{hit.code}</span>
                </Command.Item>
                <Command.Item value={hit.code + ":research"} className={itemStyle} onSelect={() => run(() => onResearch(hit))}>
                  <FlaskConical className="size-4 text-gold" /> 研究
                </Command.Item>
              </div>
            ))}
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
