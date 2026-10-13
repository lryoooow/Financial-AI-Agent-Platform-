import { useEffect, useRef, useState } from "react";
import { motion } from "motion/react";
import { Clock, Download, Eye, FileText, Loader2, Plus, Trash2 } from "lucide-react";
import { api, type ReportDoc, type ReportListItem } from "../../api/client";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";

export interface ReportItem {
  id: string;
  name: string;
  code: string;
  template: string;
  date: string;
  summary: string;
}

const TEMPLATE_LABEL: Record<string, string> = {
  equity_deep: "个股深度",
  industry: "行业研究",
  earnings: "财报点评",
  risk: "风险评估",
};

const FORMATS: { fmt: "docx" | "pdf" | "txt" | "md"; label: string }[] = [
  { fmt: "docx", label: "Word" },
  { fmt: "pdf", label: "PDF" },
  { fmt: "txt", label: "TXT" },
  { fmt: "md", label: "MD" },
];

async function downloadReport(id: string, fmt: "docx" | "pdf" | "txt" | "md") {
  try {
    const r = await fetch(api.reportDownloadUrl(id, fmt), { cache: "no-store" });
    if (!r.ok) {
      let detail = "";
      try {
        detail = (await r.json())?.detail || "";
      } catch {}
      throw new Error(detail || `download failed: ${r.status}`);
    }
    const blob = await r.blob();
    if (!blob.size) throw new Error("downloaded file is empty");
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    const filename = m ? decodeURIComponent(m[1]) : `${id}.${fmt}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    window.alert(`下载失败：${e instanceof Error ? e.message : String(e)}`);
  }
}

function ReportPreview({ report }: { report: ReportDoc }) {
  return (
    <div className="mx-auto max-w-3xl space-y-7 px-5 py-6 sm:px-8">
      <section className="border-b border-gold/30 pb-5">
        <div className="mb-2 text-xs text-gold">投资摘要</div>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/90">{report.summary || "（无摘要）"}</p>
        {report.catalyst && (
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            <span className="text-foreground/70">催化剂：</span>{report.catalyst}
          </p>
        )}
      </section>

      {report.financials.rows.length > 0 && (
        <section>
          <h2 className="mb-3 border-l-[3px] border-gold pl-2.5 text-sm font-semibold">财务分析</h2>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-secondary/50">
                  <th className="px-3 py-2 text-left font-normal text-muted-foreground">指标</th>
                  {report.financials.years.map((year) => (
                    <th key={year} className="px-3 py-2 font-normal text-muted-foreground">{year}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {report.financials.rows.map((row, index) => (
                  <tr key={row.name} className={index % 2 ? "bg-secondary/20" : ""}>
                    <td className="px-3 py-2 text-foreground/80">{row.name}</td>
                    {row.values.map((value, valueIndex) => (
                      <td key={valueIndex} className="px-3 py-2 text-center text-foreground/90">{value}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {report.sections.map((section) => (
        <section key={section.title}>
          <h2 className="mb-2 border-l-[3px] border-gold pl-2.5 text-sm font-semibold">{section.title}</h2>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/80">{section.body}</p>
        </section>
      ))}

      {report.risks.length > 0 && (
        <section>
          <h2 className="mb-2 border-l-[3px] border-destructive pl-2.5 text-sm font-semibold">风险提示</h2>
          <ul className="space-y-1.5 text-sm leading-relaxed text-foreground/80">
            {report.risks.map((risk) => <li key={risk}>· {risk}</li>)}
          </ul>
        </section>
      )}

      {report.sources.length > 0 && (
        <section>
          <h2 className="mb-2 border-l-[3px] border-border pl-2.5 text-sm font-semibold">引用来源</h2>
          <ol className="space-y-1.5 text-xs text-muted-foreground">
            {report.sources.map((source, index) => (
              <li key={`${source.title}-${index}`}>
                <span className="mr-2 text-foreground/70">[{index + 1}]</span>
                {source.title}
                {source.date && <span className="ml-2 text-muted-foreground/60">{source.date}</span>}
              </li>
            ))}
          </ol>
        </section>
      )}

      {report.disclaimer && (
        <p className="border-t border-border pt-4 text-[11px] leading-relaxed text-muted-foreground/70">{report.disclaimer}</p>
      )}
    </div>
  );
}

export function ReportsLibrary({ tick, onNew }: { tick?: number; onNew: () => void }) {
  const [items, setItems] = useState<ReportListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [previewItem, setPreviewItem] = useState<ReportListItem | null>(null);
  const [previewDoc, setPreviewDoc] = useState<ReportDoc | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const previewRequest = useRef(0);
  const [confirmItem, setConfirmItem] = useState<ReportListItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  // 自动刷新：报告生成中每 15 秒拉一次，确保新报告及时出现
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (tick !== undefined) return; // 已有外部触发时跳过
      refresh();
    }, 15000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api.reportsList()
      .then((r) => { if (!cancelled) setItems(r.reports || []); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tick]);

  async function openPreview(item: ReportListItem) {
    const requestId = ++previewRequest.current;
    setPreviewItem(item);
    setPreviewDoc(null);
    setPreviewError("");
    setPreviewLoading(true);
    try {
      const doc = await api.report(item.id);
      if (previewRequest.current === requestId) setPreviewDoc(doc);
    } catch (e) {
      if (previewRequest.current === requestId) {
        setPreviewError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (previewRequest.current === requestId) setPreviewLoading(false);
    }
  }

  function closePreview() {
    previewRequest.current += 1;
    setPreviewItem(null);
    setPreviewDoc(null);
    setPreviewError("");
    setPreviewLoading(false);
  }

  async function handleDelete() {
    if (!confirmItem || deleting) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await api.deleteReport(confirmItem.id);
      setItems((prev) => prev.filter((it) => it.id !== confirmItem.id));
      setConfirmItem(null);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-5xl">
        <div className="mb-5 flex items-center justify-between">
          <div>
            <h1 className="font-display" style={{ fontSize: 22, fontWeight: 600 }}>报告库</h1>
            <p className="mt-1 text-sm text-muted-foreground">本地 SQLite 保存 · 共 {items.length} 份研究报告 · 支持应用内查看与文件导出</p>
          </div>
          <button onClick={onNew} className="flex items-center gap-1.5 rounded-lg bg-gold px-3.5 py-2 text-sm text-[#17140a] transition-all hover:brightness-110">
            <Plus className="size-4" strokeWidth={2.4} /> 新建研究
          </button>
        </div>

        {error && <div className="mb-4 text-xs text-destructive">报告库加载失败：{error}</div>}

        {loading && !items.length ? (
          <div className="grid place-items-center py-24 text-muted-foreground"><Loader2 className="size-6 animate-spin text-gold" /></div>
        ) : items.length === 0 ? (
          <div className="grid place-items-center py-24 text-center">
            <div>
              <div className="mx-auto mb-4 grid size-14 place-items-center rounded-2xl bg-secondary">
                <FileText className="size-6 text-muted-foreground" />
              </div>
              <p className="text-sm text-muted-foreground">还没有报告，去「研究任务」生成第一份。</p>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {items.map((report, index) => (
              <motion.div
                key={report.id}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: index * 0.04, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
                className="group rounded-xl border border-border bg-card p-4 transition-colors hover:border-gold/40"
              >
                <div className="flex items-start gap-2">
                  <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent">
                    <FileText className="size-4 text-gold" />
                  </div>
                  <div className="min-w-0 flex-1 leading-tight">
                    <div className="flex items-center gap-2 text-sm text-foreground">
                      <span className="truncate">{report.name || report.ticker}</span>
                      <span className="shrink-0 text-[10px] text-muted-foreground tabular">{report.ticker}</span>
                    </div>
                    <div className="text-[11px] text-gold/80">{TEMPLATE_LABEL[report.template] || report.template}</div>
                  </div>
                </div>
                <p className="mt-3 line-clamp-2 min-h-[2.4em] text-xs leading-relaxed text-muted-foreground">{report.summary || "（无摘要）"}</p>
                <div className="mt-3 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/70">
                    <Clock className="size-3" /> {report.as_of || report.created_at}
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => openPreview(report)}
                      title="查看报告"
                      className="grid size-6 place-items-center rounded bg-secondary/70 text-foreground/80 transition-colors hover:bg-gold hover:text-[#17140a]"
                    >
                      <Eye className="size-3.5" />
                    </button>
                    <button
                      onClick={() => { setConfirmItem(report); setDeleteError(""); }}
                      title="删除报告（真删除，不可恢复）"
                      className="grid size-6 place-items-center rounded bg-secondary/70 text-foreground/80 transition-colors hover:bg-destructive hover:text-white"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                    <Download className="mr-0.5 size-3 text-muted-foreground/60" />
                    {FORMATS.map((format) => (
                      <button
                        key={format.fmt}
                        onClick={() => downloadReport(report.id, format.fmt)}
                        title={`下载 ${format.label}`}
                        className="rounded bg-secondary/70 px-1.5 py-0.5 text-[10px] text-foreground/80 transition-colors hover:bg-gold hover:text-[#17140a]"
                      >
                        {format.label}
                      </button>
                    ))}
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </div>

      <Dialog open={!!previewItem} onOpenChange={(open) => { if (!open) closePreview(); }}>
        <DialogContent className="flex h-[calc(100vh-3rem)] max-w-5xl flex-col gap-0 overflow-hidden p-0 sm:max-w-5xl">
          <DialogHeader className="shrink-0 border-b border-border px-5 py-4 pr-12 sm:px-8">
            <DialogTitle>{previewItem?.name || previewItem?.ticker || "研究报告"}</DialogTitle>
            <DialogDescription>
              {previewItem ? `${TEMPLATE_LABEL[previewItem.template] || previewItem.template} · ${previewItem.as_of || previewItem.created_at}` : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {previewLoading && <div className="grid place-items-center py-24"><Loader2 className="size-6 animate-spin text-gold" /></div>}
            {previewError && <div className="px-5 py-6 text-sm text-destructive sm:px-8">报告读取失败：{previewError}</div>}
            {previewDoc && <ReportPreview report={previewDoc} />}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!confirmItem} onOpenChange={(open) => { if (!open && !deleting) { setConfirmItem(null); setDeleteError(""); } }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>删除报告</DialogTitle>
            <DialogDescription>
              确定删除「{confirmItem?.name || confirmItem?.ticker}」？这是真删除：报告内容、生成它的研究任务及中间状态会被永久移除，不可恢复。
            </DialogDescription>
          </DialogHeader>
          {deleteError && <div className="text-xs text-destructive">删除失败：{deleteError}</div>}
          <div className="mt-2 flex justify-end gap-2">
            <button
              onClick={() => { setConfirmItem(null); setDeleteError(""); }}
              disabled={deleting}
              className="rounded-lg bg-secondary px-3.5 py-2 text-sm text-foreground/80 transition-colors hover:bg-secondary/70 disabled:opacity-50"
            >
              取消
            </button>
            <button
              onClick={handleDelete}
              disabled={deleting}
              className="flex items-center gap-1.5 rounded-lg bg-destructive px-3.5 py-2 text-sm text-white transition-all hover:brightness-110 disabled:opacity-50"
            >
              {deleting ? <Loader2 className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              {deleting ? "删除中…" : "确认删除"}
            </button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
