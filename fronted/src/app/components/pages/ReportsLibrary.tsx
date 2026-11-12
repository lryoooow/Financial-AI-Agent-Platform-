import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import { BookOpen, Clock, Download, ExternalLink, Eye, FileText, Loader2, MessagesSquare, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { api, downloadReportFile, type KnowledgeHit, type KnowledgeStats, type ReportDoc, type ReportListItem } from "../../api/client";
import { StructuredText } from "../../lib/markdown";
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

async function downloadReport(id: string, fmt: "docx" | "pdf" | "txt" | "md", onError?: (msg: string) => void) {
  try {
    await downloadReportFile(id, fmt);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (onError) onError(msg);
  }
}

function ReportPreview({ report }: { report: ReportDoc }) {
  const finYears = report.financials?.years ?? [];
  const finRows = report.financials?.rows ?? [];
  const sections = report.sections ?? [];
  const risks = report.risks ?? [];
  const sources = report.sources ?? [];

  return (
    <div className="mx-auto max-w-3xl space-y-7 px-5 py-6 sm:px-8">
      {report.llm_degraded && (
        <div className="rounded-lg border border-gold/30 bg-gold/10 px-3 py-2 text-xs text-gold">
          提示：本报告生成过程中模型服务不可用或触发限流，部分章节由本地规则兜底生成。
        </div>
      )}
      <section className="border-b border-gold/30 pb-5">
        <div className="mb-2 text-xs text-gold">投资摘要</div>
        <div className="text-sm leading-relaxed text-foreground/90">
          <StructuredText content={report.summary || "（无摘要）"} />
        </div>
        {report.catalyst && (
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            <span className="text-foreground/70">催化剂：</span>{report.catalyst}
          </p>
        )}
      </section>

      {finRows.length > 0 && finYears.length > 0 && (
        <section>
          <h2 className="mb-3 border-l-[3px] border-gold pl-2.5 text-sm font-semibold">财务分析</h2>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-secondary/50">
                  <th className="px-3 py-2 text-left font-normal text-muted-foreground">指标</th>
                  {finYears.map((year) => (
                    <th key={year} className="px-3 py-2 font-normal text-muted-foreground">{year}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {finRows.map((row, index) => (
                  <tr key={row.name} className={index % 2 ? "bg-secondary/20" : ""}>
                    <td className="px-3 py-2 text-foreground/80">{row.name}</td>
                    {(row.values || []).map((value, valueIndex) => (
                      <td key={valueIndex} className="px-3 py-2 text-center text-foreground/90">{value}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {sections.map((section) => (
        <section key={section.title}>
          <h2 className="mb-2 border-l-[3px] border-gold pl-2.5 text-sm font-semibold">{section.title}</h2>
          <div className="text-sm leading-relaxed text-foreground/80">
            <StructuredText content={section.body || ""} />
          </div>
        </section>
      ))}

      {risks.length > 0 && (
        <section>
          <h2 className="mb-2 border-l-[3px] border-destructive pl-2.5 text-sm font-semibold">风险提示</h2>
          <ul className="space-y-1.5 text-sm leading-relaxed text-foreground/80">
            {risks.map((risk, idx) => <li key={`${idx}-${risk}`}>· {risk}</li>)}
          </ul>
        </section>
      )}

      {sources.length > 0 && (
        <section>
          <h2 className="mb-2 border-l-[3px] border-border pl-2.5 text-sm font-semibold">引用来源</h2>
          <ol className="space-y-1.5 text-xs text-muted-foreground">
            {sources.map((source, index) => {
              const extUrl = /^https?:\/\//i.test(source.url || "") ? source.url : "";
              return (
                <li key={`${source.title}-${index}`} className="flex items-center gap-1.5 flex-wrap">
                  <span className="mr-1 text-foreground/70">[{index + 1}]</span>
                  {extUrl ? (
                    <a
                      href={extUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:text-gold hover:underline inline-flex items-center gap-1"
                    >
                      <span>{source.title}</span>
                      <ExternalLink className="size-3 shrink-0 opacity-70" />
                    </a>
                  ) : (
                    <span>{source.title}</span>
                  )}
                  {source.date && <span className="ml-1 text-muted-foreground/60">{source.date}</span>}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {report.disclaimer && (
        <p className="border-t border-border pt-4 text-[11px] leading-relaxed text-muted-foreground/70">{report.disclaimer}</p>
      )}
    </div>
  );
}

export function ReportsLibrary({
  tick,
  visible = true,
  onNew,
}: {
  tick?: number;
  visible?: boolean;
  onNew: () => void;
}) {
  const [items, setItems] = useState<ReportListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [exportError, setExportError] = useState("");
  const [filterText, setFilterText] = useState("");
  const [manualTick, setManualTick] = useState(0);
  const [previewItem, setPreviewItem] = useState<ReportListItem | null>(null);
  const [previewDoc, setPreviewDoc] = useState<ReportDoc | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const previewRequest = useRef(0);
  const [confirmItem, setConfirmItem] = useState<ReportListItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  // v1.8.0：知识库检索（所有研究报告自动入库，与对话 agent 的 search_knowledge 是同一套服务）
  const [kbQuery, setKbQuery] = useState("");
  const [kbHits, setKbHits] = useState<KnowledgeHit[] | null>(null);
  const [kbLoading, setKbLoading] = useState(false);
  const [kbError, setKbError] = useState("");
  const [kbStats, setKbStats] = useState<KnowledgeStats | null>(null);

  useEffect(() => {
    if (!visible) return;
    api.knowledgeStats().then(setKbStats).catch(() => {});
  }, [tick, visible, manualTick, items.length]);

  async function searchKnowledge() {
    const q = kbQuery.trim();
    if (!q) { setKbHits(null); return; }
    setKbLoading(true);
    setKbError("");
    try {
      const r = await api.knowledgeSearch(q, { limit: 12 });
      setKbHits(r.items || []);
    } catch (e) {
      setKbError(e instanceof Error ? e.message : String(e));
    } finally {
      setKbLoading(false);
    }
  }

  function openHit(hit: KnowledgeHit) {
    if (!hit.report_id) return;
    const known = items.find((it) => it.id === hit.report_id);
    void openPreview(known ?? {
      id: hit.report_id, task_id: "", ticker: hit.ticker, name: hit.name, template: hit.template,
      summary: "", as_of: hit.as_of, created_at: hit.as_of,
    } as ReportListItem);
  }

  useEffect(() => {
    if (!visible && items.length > 0 && manualTick === 0) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    api.reportsList()
      .then((r) => { if (!cancelled) setItems(r.reports || []); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tick, visible, manualTick]);

  const filteredItems = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (it) =>
        (it.name || "").toLowerCase().includes(q) ||
        (it.ticker || "").toLowerCase().includes(q) ||
        (TEMPLATE_LABEL[it.template] || it.template || "").toLowerCase().includes(q) ||
        (it.summary || "").toLowerCase().includes(q),
    );
  }, [items, filterText]);

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
      const deletedId = confirmItem.id;
      const deletedTaskId = confirmItem.task_id;
      await api.deleteReport(deletedId);
      setItems((prev) => prev.filter((it) => it.id !== deletedId));
      setKbHits((prev) => (prev ? prev.filter((h) => h.report_id !== deletedId) : null));
      if (previewItem?.id === deletedId) closePreview();
      window.dispatchEvent(new CustomEvent("finresearch:report-deleted", { detail: { id: deletedId, taskId: deletedTaskId } }));
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
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-display" style={{ fontSize: 22, fontWeight: 600 }}>报告库</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              本地 SQLite 保存 · 共 {items.length} 份研究报告 · 全部自动进入本地知识库
              {kbStats ? `（${kbStats.reports} 份报告 / ${kbStats.chunks} 个片段${kbStats.conversations ? ` / ${kbStats.conversations} 段对话摘要` : ""}）` : ""}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs">
              <Search className="size-3.5 text-muted-foreground shrink-0" />
              <input
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
                placeholder="筛选标题/代码…"
                className="w-32 sm:w-44 bg-transparent outline-none placeholder:text-muted-foreground"
              />
              {filterText && (
                <button type="button" onClick={() => setFilterText("")} className="text-muted-foreground hover:text-foreground">
                  <X className="size-3" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => setManualTick((t) => t + 1)}
              disabled={loading}
              title="刷新报告列表"
              className="flex items-center gap-1 rounded-lg border border-border bg-card px-2.5 py-2 text-xs text-foreground/85 hover:bg-secondary/60 disabled:opacity-50 transition-colors"
            >
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} /> 刷新
            </button>
            <button onClick={onNew} className="flex items-center gap-1.5 rounded-lg bg-gold px-3.5 py-2 text-sm text-[#17140a] transition-all hover:brightness-110">
              <Plus className="size-4" strokeWidth={2.4} /> 新建深度研究
            </button>
          </div>
        </div>

        <div className="mb-5 rounded-xl border border-border bg-card p-3">
          <div className="flex items-center gap-2 rounded-lg bg-input px-3 py-2 border border-transparent focus-within:border-gold/40">
            <BookOpen className="size-4 text-gold shrink-0" />
            <input
              value={kbQuery}
              onChange={(e) => { setKbQuery(e.target.value); if (!e.target.value.trim()) setKbHits(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) void searchKnowledge(); }}
              placeholder="检索知识库：如「茅台 估值」「现金流」「光伏 景气」——与研究对话中的 AI 检索同一套知识库"
              className="bg-transparent outline-none text-sm w-full placeholder:text-muted-foreground"
            />
            {kbLoading ? <Loader2 className="size-4 animate-spin text-muted-foreground" /> : (
              <button onClick={() => void searchKnowledge()} className="grid place-items-center size-7 rounded-md hover:bg-secondary text-muted-foreground" title="检索">
                <Search className="size-4" />
              </button>
            )}
            {kbHits && (
              <button onClick={() => { setKbHits(null); setKbQuery(""); }} className="grid place-items-center size-7 rounded-md hover:bg-secondary text-muted-foreground" title="清除">
                <X className="size-3.5" />
              </button>
            )}
          </div>
          {kbError && <div className="mt-2 text-xs text-destructive">检索失败：{kbError}</div>}
          {kbHits && (
            <div className="mt-3 space-y-2">
              {kbHits.length === 0 && <div className="py-4 text-center text-xs text-muted-foreground">知识库中没有相关内容</div>}
              {kbHits.map((h) => (
                <button
                  key={`${h.doc_id}-${h.heading}-${h.score}`}
                  onClick={() => openHit(h)}
                  disabled={!h.report_id}
                  className="w-full text-left rounded-lg border border-border/70 bg-secondary/20 px-3 py-2 hover:border-gold/40 disabled:cursor-default disabled:hover:border-border/70 transition-colors"
                >
                  <div className="flex items-center gap-2 text-xs">
                    {h.kind === "report" ? <FileText className="size-3.5 text-gold" /> : <MessagesSquare className="size-3.5 text-muted-foreground" />}
                    <span className="text-foreground/90 truncate">{h.title}</span>
                    <span className="text-muted-foreground shrink-0">· {h.heading}</span>
                    <span className="ml-auto text-[10px] text-muted-foreground/70 shrink-0">{h.as_of}</span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">{h.text}</p>
                </button>
              ))}
            </div>
          )}
        </div>

        {error && <div className="mb-4 text-xs text-destructive">报告库加载失败：{error}</div>}
        {exportError && (
          <div className="mb-4 flex items-center justify-between rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <span>导出失败：{exportError}</span>
            <button type="button" onClick={() => setExportError("")}><X className="size-3.5" /></button>
          </div>
        )}

        {loading && !items.length ? (
          <div className="grid place-items-center py-24 text-muted-foreground"><Loader2 className="size-6 animate-spin text-gold" /></div>
        ) : filteredItems.length === 0 ? (
          <div className="grid place-items-center py-24 text-center">
            <div>
              <div className="mx-auto mb-4 grid size-14 place-items-center rounded-2xl bg-secondary">
                <FileText className="size-6 text-muted-foreground" />
              </div>
              <p className="text-sm text-muted-foreground">
                {items.length === 0
                  ? "还没有报告，前往左侧导航「深度研究」一键生成第一份深度研报。"
                  : `未找到匹配「${filterText}」的报告`}
              </p>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {filteredItems.map((report, index) => (
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
                        onClick={() => { setExportError(""); void downloadReport(report.id, format.fmt, setExportError); }}
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
