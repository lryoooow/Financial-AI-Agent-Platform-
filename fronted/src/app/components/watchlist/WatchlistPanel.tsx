import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, GripVertical, Pencil, Plus, ScanLine, Trash2 } from "lucide-react";
import { api, type HumanApproval, type Quote, type RadarRow, type WatchlistGroup, type WatchlistItem } from "../../api/client";
import { fmt, signed, trendClass } from "../../lib/format";
import { loadActiveLlm } from "../ModelPicker";
import { useConfirm } from "../ui/confirm";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Skeleton } from "../ui/skeleton";
import { RadarTable } from "./RadarTable";
import { WatchlistImport } from "./WatchlistImport";

const WATCHLIST_UPDATED = "jingyuan:watchlist-updated";
const toolbarButton = "rounded border px-2 py-1 hover:bg-accent disabled:opacity-50";

export function WatchlistPanel({ visible, compact = false, currentSymbol, onSelect, onQueued }: {
  visible: boolean; compact?: boolean; currentSymbol?: string; onSelect: (symbol: string) => void; onQueued?: () => void;
}) {
  const confirm = useConfirm();
  const [groups, setGroups] = useState<WatchlistGroup[]>();
  const [groupId, setGroupId] = useState("");
  const [quotes, setQuotes] = useState<Record<string, Partial<Quote>>>({});
  const [importOpen, setImportOpen] = useState(false);
  const [editor, setEditor] = useState<{ kind: "group" | "item"; id?: string }>();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [radar, setRadar] = useState<RadarRow[]>();
  const [scanning, setScanning] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [approval, setApproval] = useState<{ conversationId: string; card: HumanApproval }>();
  const dragging = useRef("");
  const scan = useRef<AbortController | null>(null);
  const group = groups?.find((item) => item.id === groupId) ?? groups?.[0];
  const selectedGroupId = group?.id ?? "";
  const quoteKey = group?.items.map((item) => item.symbol).join(",");

  const refresh = useCallback(() => {
    api.watchlist().then(({ groups }) => setGroups(groups))
      .catch((cause) => setError(cause instanceof Error ? cause.message : String(cause)));
  }, []);
  const saved = () => window.dispatchEvent(new Event(WATCHLIST_UPDATED));

  useEffect(() => {
    if (!visible) return;
    refresh();
    window.addEventListener(WATCHLIST_UPDATED, refresh);
    return () => window.removeEventListener(WATCHLIST_UPDATED, refresh);
  }, [refresh, visible]);

  useEffect(() => {
    setQuotes({});
    if (!visible || !selectedGroupId || !quoteKey) return;
    const controller = new AbortController();
    let loading = false;
    const load = () => {
      if (document.hidden || loading) return;
      loading = true;
      api.watchlistQuotes(selectedGroupId, controller.signal).then(({ quotes }) => {
        if (!controller.signal.aborted) setQuotes(quotes);
      }).catch(() => {}).finally(() => { loading = false; });
    };
    load();
    const timer = window.setInterval(load, 45_000);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [selectedGroupId, quoteKey, visible]);

  useEffect(() => {
    setRadar(undefined);
    setSelected([]);
    return () => { scan.current?.abort(); scan.current = null; setScanning(false); };
  }, [selectedGroupId, quoteKey]);

  async function mutate(operation: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await operation();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  function edit(kind: "group" | "item", value: string, id?: string) {
    setDraft(value);
    setEditor({ kind, id });
  }

  async function saveEditor() {
    if (!editor) return;
    await mutate(async () => {
      if (editor.kind === "group") {
        if (editor.id) await api.updateWatchlist({ ...editor, name: draft });
        else {
          const result = await api.addWatchlist({ kind: "group", name: draft });
          if (result.id) setGroupId(result.id);
        }
      } else await api.updateWatchlist({ ...editor, note: draft });
      setEditor(undefined);
      saved();
    });
  }

  async function reorder(from: string, to: string) {
    if (!group || from === to) return;
    const ids = group.items.map((item) => item.id);
    const start = ids.indexOf(from);
    const end = ids.indexOf(to);
    if (start < 0 || end < 0) return;
    ids.splice(start, 1);
    ids.splice(end, 0, from);
    await mutate(async () => { await api.updateWatchlist({ kind: "order", group_id: group.id, ids }); saved(); });
  }

  async function runRadar() {
    if (!group || scan.current) return;
    const controller = new AbortController();
    scan.current = controller;
    setScanning(true);
    setError("");
    setRadar(undefined);
    setSelected([]);
    try {
      const result = await api.watchlistRadar(group.id, controller.signal);
      if (!controller.signal.aborted) setRadar(result.rows);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (scan.current === controller) { scan.current = null; setScanning(false); }
    }
  }

  async function queue() {
    await mutate(async () => {
      const result = await api.batchResearch(selected, loadActiveLlm() ?? undefined);
      if (result.approval && result.conversation_id) setApproval({ conversationId: result.conversation_id, card: result.approval });
      else {
        setMessage("已排入 " + (result.task_ids?.length ?? 0) + " 项深度研究。");
        setSelected([]);
        onQueued?.();
      }
    });
  }

  async function decide(action: "approve" | "reject") {
    if (!approval) return;
    await mutate(async () => {
      const result = await api.approveBatch(approval.conversationId, approval.card.checkpoint_id, action);
      setApproval(undefined);
      setSelected([]);
      setMessage(action === "reject" ? "已取消批量研究。" : "已排入 " + (result.task_ids?.length ?? 0) + " 项深度研究。");
      onQueued?.();
    });
  }

  async function remove(item?: WatchlistItem) {
    if (!group) return;
    const title = item ? "移除 " + item.name + "？" : "删除分组 " + group.name + "？";
    if (!await confirm(title)) return;
    await mutate(async () => { await api.removeWatchlist(item?.id ?? group.id, item ? "item" : "group"); saved(); });
  }

  return <section aria-label="自选股" className="min-w-0 space-y-2 rounded-lg border bg-card p-2.5">
    <div className="flex flex-wrap items-center gap-1.5">
      <h2 className="mr-1 font-medium">自选股</h2>
      {group && <select aria-label="自选分组" className="min-w-0 max-w-36 rounded border bg-input px-1.5 py-1" value={group.id}
        onChange={(event) => setGroupId(event.target.value)}>
        {groups?.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
      </select>}
      <button type="button" title="新建自选分组" aria-label="新建自选分组" className={toolbarButton} onClick={() => edit("group", "")}><Plus className="size-3.5" /></button>
      {group && <>
        <button type="button" title="重命名分组" aria-label="重命名分组" className={toolbarButton} onClick={() => edit("group", group.name, group.id)}><Pencil className="size-3.5" /></button>
        <button type="button" title="删除分组" aria-label="删除分组" disabled={busy || (groups?.length ?? 0) <= 1} className={toolbarButton} onClick={() => void remove()}><Trash2 className="size-3.5" /></button>
        <button type="button" className={toolbarButton} onClick={() => setImportOpen(true)}>导入</button>
        {currentSymbol && !group.items.some((item) => item.symbol === currentSymbol) && <button type="button" className={toolbarButton} disabled={busy}
          onClick={() => void mutate(async () => { await api.addWatchlist({ group_id: group.id, symbol: currentSymbol }); saved(); })}>加入当前标的</button>}
        {!compact && <button type="button" className={toolbarButton + " ml-auto flex items-center gap-1"} disabled={scanning || !group.items.length} onClick={() => void runRadar()}>
          <ScanLine className="size-3.5" /> {scanning ? "体检中…" : "五维体检"}
        </button>}
      </>}
    </div>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {message && <p role="status" className="text-xs text-gold">{message}</p>}
    {!groups && !error && <Skeleton className="h-24" />}
    {group && !group.items.length && <p className="p-3 text-muted-foreground">分组为空。导入或从看盘页加入标的。</p>}
    {!!group?.items.length && <div className="overflow-auto">
      <table className="financial-table text-xs">
        <thead><tr><th>标的</th><th>现价</th><th>涨跌</th>{!compact && <><th>PE</th><th>PB</th><th>加入价</th><th>较加入价</th></>}<th>编辑</th></tr></thead>
        <tbody>{group.items.map((item, index) => {
          const quote = quotes[item.symbol];
          const price = quote?.price && quote.price > 0 ? quote.price : null;
          const change = price != null ? quote?.changePct : null;
          const delta = price != null && item.added_price ? (price / item.added_price - 1) * 100 : null;
          return <tr key={item.id} onDragOver={(event) => { if (dragging.current) event.preventDefault(); }}
            onDrop={(event) => { event.preventDefault(); void reorder(dragging.current, item.id); dragging.current = ""; }}>
            <td><div className="flex items-center gap-1">
              <span draggable={!busy} onDragStart={(event) => { dragging.current = item.id; event.dataTransfer.setData("text/plain", item.id); }}
                onDragEnd={() => { dragging.current = ""; }} title="拖动调整顺序" className="cursor-grab text-muted-foreground"><GripVertical className="size-3" /></span>
              <button type="button" onClick={() => onSelect(item.symbol)} className="whitespace-nowrap text-left hover:text-gold" title={item.note || item.symbol}>
                {item.name}{!compact && <span className="ml-2 font-mono text-muted-foreground">{item.symbol}</span>}
              </button>
            </div></td>
            <td title={quote?.as_of}>{price == null ? "—" : fmt(price)}</td>
            <td className={trendClass(change ?? 0)}>{change == null ? "—" : signed(change) + "%"}</td>
            {!compact && <><td>{price && quote?.pe ? fmt(quote.pe) : "—"}</td><td>{price && quote?.pb ? fmt(quote.pb) : "—"}</td>
              <td>{item.added_price == null ? "—" : fmt(item.added_price)}</td><td className={trendClass(delta ?? 0)}>{delta == null ? "—" : signed(delta) + "%"}</td></>}
            <td><div className="flex justify-end gap-1">
              {!compact && <>
                <button type="button" aria-label={"上移 " + item.name} disabled={!index || busy} onClick={() => void reorder(item.id, group.items[index - 1].id)} className="disabled:opacity-30"><ArrowUp className="size-3" /></button>
                <button type="button" aria-label={"下移 " + item.name} disabled={index === group.items.length - 1 || busy} onClick={() => void reorder(item.id, group.items[index + 1].id)} className="disabled:opacity-30"><ArrowDown className="size-3" /></button>
              </>}
              <button type="button" aria-label={"编辑 " + item.name + " 备注"} onClick={() => edit("item", item.note, item.id)}><Pencil className="size-3" /></button>
              <button type="button" aria-label={"移除 " + item.name} disabled={busy} onClick={() => void remove(item)}><Trash2 className="size-3" /></button>
            </div></td>
          </tr>;
        })}</tbody>
      </table>
    </div>}
    {scanning && <div role="status" className="space-y-2"><p>正在核验估值、财报、量价、风险和快讯。</p><Skeleton className="h-24" /></div>}
    {radar && <>
      <RadarTable rows={radar} selected={selected} onSelect={onSelect}
        onToggle={(symbol) => setSelected((items) => items.includes(symbol) ? items.filter((item) => item !== symbol) : [...items, symbol])} />
      <button type="button" disabled={busy || !selected.length || !!approval} className={toolbarButton} onClick={() => void queue()}>排入深度研究 · {selected.length} 只</button>
    </>}
    {group && <WatchlistImport groupId={group.id} open={importOpen} onOpenChange={setImportOpen} onSaved={saved} />}
    <Dialog open={!!editor} onOpenChange={(open) => { if (!open && !busy) setEditor(undefined); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>{editor?.kind === "group" ? "自选分组" : "自选备注"}</DialogTitle><DialogDescription>修改会保存到本机自选列表。</DialogDescription></DialogHeader>
        <input aria-label={editor?.kind === "group" ? "分组名称" : "备注"} className="rounded border bg-input p-2"
          maxLength={editor?.kind === "group" ? 64 : 200} value={draft} onChange={(event) => setDraft(event.target.value)} />
        {error && <p role="alert" className="text-destructive">{error}</p>}
        <button type="button" className="justify-self-end rounded bg-action px-3 py-1 text-gold-foreground disabled:opacity-50"
          disabled={busy || (editor?.kind === "group" && !draft.trim())} onClick={() => void saveEditor()}>保存</button>
      </DialogContent>
    </Dialog>
    <Dialog open={!!approval} onOpenChange={(open) => { if (!open && !busy) setApproval(undefined); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>{approval?.card.label}</DialogTitle><DialogDescription>确认后将按并发上限依次执行，可在深度研究中取消单项。</DialogDescription></DialogHeader>
        <div className="max-h-56 overflow-auto text-sm">{approval?.card.candidates.map((item) => <p key={item.code}>{item.name} · {item.code}</p>)}</div>
        {error && <p role="alert" className="text-destructive">{error}</p>}
        <div className="flex justify-end gap-2"><button type="button" className={toolbarButton} disabled={busy} onClick={() => void decide("reject")}>取消批次</button>
          <button type="button" className="rounded bg-action px-3 py-1 text-gold-foreground" disabled={busy} onClick={() => void decide("approve")}>确认提交</button></div>
      </DialogContent>
    </Dialog>
  </section>;
}
