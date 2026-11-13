import { useState, type DragEvent } from "react";
import { Upload } from "lucide-react";
import { api, type WatchlistPreview } from "../../api/client";
import { isImeKeyEvent } from "../../lib/ime";
import { loadActiveLlm } from "../ModelPicker";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";

function dataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("无法读取文件。"));
    reader.readAsDataURL(file);
  });
}

export function WatchlistImport({ groupId, open, onOpenChange, onSaved }: {
  groupId: string; open: boolean; onOpenChange: (value: boolean) => void; onSaved: () => void;
}) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<WatchlistPreview>();
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function parse(file?: File) {
    if (busy || (!file && !text.trim())) return;
    setBusy(true);
    setError("");
    setPreview(undefined);
    try {
      let result;
      if (file) {
        if (file.size > 12 * 1024 * 1024) throw new Error("文件超过 12 MiB。");
        const image = file.type.startsWith("image/");
        const encoded = await dataUrl(file);
        result = await api.watchlistPreview({ mode: image ? "image" : "table", payload: image ? encoded : encoded.split(",", 2)[1],
          filename: file.name, llm: image ? loadActiveLlm() ?? undefined : undefined });
      } else {
        result = await api.watchlistPreview({ mode: "text", payload: text });
      }
      setPreview(result);
      setSelected(result.verified.map((item) => item.code));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  function drop(event: DragEvent) {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) void parse(file);
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      for (let offset = 0; offset < selected.length; offset += 50) {
        await api.addWatchlist({ group_id: groupId, symbols: selected.slice(offset, offset + 50) });
        onSaved();
      }
      setPreview(undefined);
      setText("");
      onOpenChange(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  function check(code: string, checked: boolean) {
    setSelected((previous) => checked ? Array.from(new Set([...previous, code])) : previous.filter((item) => item !== code));
  }

  return <Dialog open={open} onOpenChange={(value) => {
    if (busy) return;
    if (!value) {
      setError("");
      setPreview(undefined);
      setSelected([]);
    }
    onOpenChange(value);
  }}>
    <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl" onPaste={(event) => {
      const file = Array.from(event.clipboardData.files).find((item) => item.type.startsWith("image/"));
      if (file) { event.preventDefault(); void parse(file); }
    }}>
      <DialogHeader><DialogTitle>导入自选股</DialogTitle><DialogDescription>选择截图、Excel 或 CSV，也可粘贴股票代码和简称。识别截图时会将图像发给当前视觉模型。</DialogDescription></DialogHeader>
      <label className="flex cursor-pointer items-center gap-2 rounded-md border border-dashed bg-secondary p-3" onDrop={drop} onDragOver={(event) => event.preventDefault()}>
        <Upload className="size-4" /> 拖入文件或点击选择
        <input type="file" className="sr-only" accept=".xlsx,.csv,.png,.jpg,.jpeg,.webp" disabled={busy}
          onChange={(event) => { const file = event.target.files?.[0]; if (file) void parse(file); event.target.value = ""; }} />
      </label>
      <textarea aria-label="股票代码和简称" className="min-h-20 resize-y rounded-md border bg-input p-2" value={text} maxLength={100000}
        onChange={(event) => setText(event.target.value)} placeholder="600519.SH、宁德时代、002594"
        onKeyDown={(event) => { if (event.key === "Enter" && event.ctrlKey && !isImeKeyEvent(event.nativeEvent)) void parse(); }} />
      <button type="button" disabled={busy || !text.trim()} onClick={() => void parse()} className="justify-self-start rounded border px-3 py-1 text-gold">{busy ? "处理中…" : "核验粘贴内容"}</button>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {preview && <div className="space-y-2">
        <table className="w-full text-sm">
          <thead className="bg-secondary text-left"><tr><th className="p-2">加入</th><th>本地简称</th><th>股票代码</th></tr></thead>
          <tbody>{preview.verified.map((item) => <tr key={item.code} className="border-b">
            <td className="p-2"><input type="checkbox" aria-label={"加入 " + item.name} checked={selected.includes(item.code)} onChange={(event) => check(item.code, event.target.checked)} /></td>
            <td>{item.name}</td><td className="font-mono">{item.code}</td>
          </tr>)}</tbody>
        </table>
        {preview.ambiguous.map((entry) => <fieldset className="rounded border p-2" key={entry.input}>
          <legend className="text-xs text-muted-foreground">待确认 · {entry.input}</legend>
          {entry.candidates.map((item) => <label key={item.code} className="mr-3 inline-flex items-center gap-1">
            <input type="checkbox" checked={selected.includes(item.code)} onChange={(event) => check(item.code, event.target.checked)} /> {item.name} {item.code}
          </label>)}
        </fieldset>)}
        {!!preview.unmatched.length && <p className="text-xs text-muted-foreground">未匹配 · {preview.unmatched.join("、")}</p>}
        <button type="button" disabled={!selected.length || busy} onClick={() => void save()} className="rounded bg-action px-3 py-1.5 text-gold-foreground disabled:opacity-50">确认加入 {selected.length} 只</button>
      </div>}
    </DialogContent>
  </Dialog>;
}
