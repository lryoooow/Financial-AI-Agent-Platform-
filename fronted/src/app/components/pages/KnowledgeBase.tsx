import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { motion } from "motion/react";
import { BookOpen, FileText, Loader2, Search, Trash2, UploadCloud } from "lucide-react";
import { api, type KbDocument, type KbSearchResult } from "../../api/client";

const ACCEPT = ".txt,.md,.pdf,.docx";
const fmtSize = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const fmtDate = (ts: number) => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false, dateStyle: "short", timeStyle: "short" });

export function KnowledgeBase() {
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<KbSearchResult[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    api.kbDocuments()
      .then((r) => { setDocs(r.documents || []); setLoaded(true); })
      .catch((e) => { setError(e instanceof Error ? e.message : String(e)); setLoaded(true); });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const upload = useCallback(async (files: File[]) => {
    setError(""); setNotice("");
    for (const file of files) {
      if (file.size > 20 * 1024 * 1024) {
        setError(`「${file.name}」超过 20MB 上限`);
        continue;
      }
      setUploading(file.name);
      try {
        const meta = await api.kbUpload(file);
        setNotice(`「${meta.document.name}」已入库：${meta.document.chunk_count} 个知识块`);
      } catch (e) {
        setError(`「${file.name}」上传失败：${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setUploading(null);
      }
    }
    refresh();
  }, [refresh]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    void upload(Array.from(e.dataTransfer.files || []));
  };

  const remove = async (doc: KbDocument) => {
    if (!window.confirm(`确定删除「${doc.name}」吗？其全部知识块将一并移除。`)) return;
    setError("");
    try {
      await api.kbDelete(doc.id);
      setNotice(`已删除「${doc.name}」`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    refresh();
  };

  const runSearch = () => {
    const q = query.trim();
    if (!q) { setResults([]); return; }
    setSearching(true);
    api.kbSearch(q, 6)
      .then((r) => setResults(r.items || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  };

  return (
    <div className="h-full overflow-y-auto p-4 lg:p-5">
      <div className="mx-auto max-w-4xl space-y-4">
        {/* 头部 */}
        <div className="flex items-center gap-2.5">
          <BookOpen className="size-5 text-gold" />
          <div>
            <h1 className="font-display text-lg font-semibold">知识库</h1>
            <p className="text-[11px] text-muted-foreground">
              上传你的研究文档（txt / md / pdf / docx），自动分块入本地向量库；对话中模型可检索引用
            </p>
          </div>
        </div>

        {/* 上传区 */}
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={`rounded-xl border-2 border-dashed p-6 text-center transition-colors ${dragOver ? "border-gold/60 bg-accent/40" : "border-border bg-card"}`}
        >
          <input
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            multiple
            hidden
            onChange={(e) => { void upload(Array.from(e.target.files || [])); e.target.value = ""; }}
          />
          <UploadCloud className="mx-auto size-7 text-muted-foreground" />
          <p className="mt-2 text-sm">拖拽文档到这里，或</p>
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading != null}
            className="mx-auto mt-2 flex items-center gap-1.5 rounded-lg bg-gold px-4 py-2 text-xs text-[#17140a] hover:brightness-110 disabled:opacity-50"
          >
            {uploading != null ? <Loader2 className="size-3.5 animate-spin" /> : <FileText className="size-3.5" />}
            {uploading != null ? `正在解析 ${uploading.slice(0, 24)}…` : "选择文件上传"}
          </button>
          <p className="mt-2 text-[10px] text-muted-foreground">单文件 ≤ 20MB；同内容重传自动替换，不会重复膨胀</p>
          {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
          {notice && <p className="mt-2 text-xs text-down">{notice}</p>}
        </div>

        {/* 检索测试 */}
        <div className="rounded-xl border border-border bg-card p-3.5">
          <div className="flex items-center gap-2">
            <Search className="size-4 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runSearch()}
              placeholder="检索测试：验证文档能否被找到（对话里模型自动检索，无需手动）"
              className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
            <button onClick={runSearch} disabled={searching || !query.trim()} className="flex items-center gap-1 rounded-md bg-secondary px-2.5 py-1.5 text-xs hover:bg-secondary/70 disabled:opacity-50">
              {searching ? <Loader2 className="size-3.5 animate-spin" /> : null} 检索
            </button>
          </div>
          {results.length > 0 && (
            <div className="mt-3 space-y-2">
              {results.map((r, i) => (
                <motion.div key={i} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className="rounded-lg bg-secondary/40 p-2.5">
                  <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                    <span className="truncate font-medium text-foreground/80">{r.doc_name}</span>
                    <span className="font-mono">{r.ref}</span>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-foreground/75">{r.text}</p>
                </motion.div>
              ))}
            </div>
          )}
        </div>

        {/* 文档列表 */}
        <div className="rounded-xl border border-border bg-card">
          <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5">
            <span className="text-sm">我的文档</span>
            <span className="text-[11px] text-muted-foreground">{docs.length} 份 · {docs.reduce((s, d) => s + d.chunk_count, 0)} 个知识块</span>
          </div>
          {!loaded ? (
            <div className="grid place-items-center py-10"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
          ) : docs.length === 0 ? (
            <div className="py-10 text-center text-xs text-muted-foreground">还没有文档。上传的第一份文档会立即可被对话检索引用。</div>
          ) : (
            <div className="divide-y divide-border">
              {docs.map((d) => (
                <div key={d.id} className="flex items-center gap-3 px-3.5 py-2.5">
                  <FileText className="size-4 shrink-0 text-gold/80" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm">{d.name}</div>
                    <div className="text-[10.5px] text-muted-foreground">
                      {d.ext.replace(".", "").toUpperCase()} · {fmtSize(d.size_bytes)} · {d.chunk_count} 块 · {fmtDate(d.added_at)}
                    </div>
                  </div>
                  <button
                    onClick={() => void remove(d)}
                    className="shrink-0 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-up/10 hover:text-up"
                    title="删除文档"
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <p className="pb-2 text-center text-[10px] leading-relaxed text-muted-foreground/70">
          所有文档只存在本机（%APPDATA%\FinResearch\kb\ 与 rag.db），不上传任何服务器。
          在对话中直接问"我知识库里关于 XX 的文档说了什么"，模型会检索并引用。
        </p>
      </div>
    </div>
  );
}
