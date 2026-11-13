import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, ExternalLink, FileText, Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { api, downloadReportFile, type KnowledgeDocument, type KnowledgeHit, type ReportDoc, type ReportFormat, type ReportListItem } from "../../api/client";
import { TEMPLATES } from "../../data/report";
import { isImeKeyEvent } from "../../lib/ime";
import { StructuredText } from "../../lib/markdown";
import { ReportLedgerPanel } from "../research/ReportLedgerPanel";
import { SharePosterView } from "../research/SharePosterView";
import { useConfirm } from "../ui/confirm";
import { Skeleton } from "../ui/skeleton";

const labels = new Map<string, string>(TEMPLATES.map(({ id, label }) => [id, label]));
const FORMATS: [ReportFormat, string][] = [["docx", "Word"], ["pdf", "PDF"], ["txt", "纯文本"], ["md", "Markdown"], ["html", "HTML"]];

function ReportPreview({ report }: { report: ReportDoc }) {
  const finYears = report.financials?.years ?? [];
  const finRows = report.financials?.rows ?? [];
  const sections = report.sections ?? [];
  const risks = report.risks ?? [];
  const sources = report.sources ?? [];

  return (
    <div className="mx-auto max-w-[760px] space-y-5 p-4">
      {report.llm_degraded && (
        <div className="rounded-lg border border-gold/30 bg-gold/10 px-3 py-2 text-xs text-gold">
          提示：本报告生成过程中模型服务不可用或触发限流，部分章节由本地规则兜底生成。
        </div>
      )}
      <section className="border-b border-gold/30 pb-5">
        <div className="mb-2 text-xs text-gold">研究摘要</div>
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
            <table className="financial-table text-xs">
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
                      <td key={valueIndex} className="px-3 py-2 text-right font-mono tabular-nums text-foreground/90">{value}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {sections.length > 1 && <nav aria-label="研报目录" className="flex flex-wrap gap-2 text-xs">
        {sections.map((section, index) => <button key={index} type="button" className="rounded border px-2 py-1 text-gold"
          onClick={() => document.getElementById("library-section-" + index)?.scrollIntoView({ block: "start" })}>{section.title}</button>)}
      </nav>}
      {sections.map((section, index) => (
        <section key={index} id={"library-section-" + index}>
          <h2 className="mb-2 border-l-[3px] border-gold pl-2.5 text-sm font-semibold">{section.title}</h2>
          <div className="text-sm leading-relaxed text-foreground/80">
            <StructuredText content={section.body || ""} citations={sources} />
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

export function ReportsLibrary({ tick, visible = true, initialQuery, initialReportId, onNew }: {
  tick?: number;
  visible?: boolean;
  initialQuery?: { query: string; nonce: number } | string;
  initialReportId?: { id: string; nonce: number } | string;
  onNew: () => void;
}) {
  const confirm = useConfirm();
  const [tab, setTab] = useState<"reports" | "knowledge">("reports");
  const [reading, setReading] = useState<"report" | "ledger">("report");
  const [reports, setReports] = useState<ReportListItem[]>();
  const [documents, setDocuments] = useState<KnowledgeDocument[]>();
  const [filter, setFilter] = useState("");
  const [template, setTemplate] = useState("");
  const [selected, setSelected] = useState("");
  const [report, setReport] = useState<ReportDoc>();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[]>();
  const [hit, setHit] = useState<KnowledgeHit>();
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [poster, setPoster] = useState(false);
  const [revision, setRevision] = useState(0);
  const handledReportNonceRef = useRef<number | string>(0);
  const handledQueryNonceRef = useRef<number | string>(0);

  function refresh() { setRevision((value) => value + 1); }
  function choose(id: string) {
    setSelected(id);
    setReading("report");
    setHit(undefined);
    setPoster(false);
  }

  useEffect(() => {
    if (!visible) return;
    let active = true;
    setError("");
    const fail = (cause: unknown) => { if (active) setError(cause instanceof Error ? cause.message : String(cause)); };
    api.reportsList().then(({ reports }) => { if (active) setReports(reports); }).catch(fail);
    api.knowledgeDocuments().then(({ items }) => { if (active) setDocuments(items); }).catch(fail);
    return () => { active = false; };
  }, [visible, tick, revision]);

  useEffect(() => {
    if (!visible || !initialReportId) return;
    const id = typeof initialReportId === "string" ? initialReportId : initialReportId.id;
    const token = typeof initialReportId === "string" ? initialReportId : initialReportId.nonce;
    if (!id || handledReportNonceRef.current === token) return;
    handledReportNonceRef.current = token;
    setTab("reports");
    choose(id);
  }, [visible, initialReportId]);

  useEffect(() => {
    setReport(undefined);
    setDetailError("");
    if (!selected) return;
    let active = true;
    api.report(selected).then((value) => { if (active) setReport(value); })
      .catch((cause) => { if (active) setDetailError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { active = false; };
  }, [selected]);

  const search = useCallback(async (value: string) => {
    if (!value.trim()) { setHits(undefined); return; }
    setSearching(true);
    setError("");
    try { setHits((await api.knowledgeSearch(value.trim(), { limit: 20 })).items); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSearching(false); }
  }, []);

  useEffect(() => {
    if (!visible || initialQuery === undefined) return;
    const qText = typeof initialQuery === "string" ? initialQuery : initialQuery.query;
    const token = typeof initialQuery === "string" ? initialQuery : initialQuery.nonce;
    if (handledQueryNonceRef.current === token) return;
    if (typeof initialQuery === "string" && !qText) return;
    handledQueryNonceRef.current = token;
    if (qText.trim()) {
      setTab("knowledge");
      setQuery(qText);
      void search(qText);
    } else {
      setTab("reports");
    }
  }, [visible, initialQuery, search]);

  async function act(operation: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  function upload(file: File) {
    void act(async () => { await api.knowledgeUpload(file); refresh(); });
  }

  const filtered = reports?.filter((item) => (!template || item.template === template) &&
    [item.ticker, item.name, item.summary].some((value) => value?.toLowerCase().includes(filter.trim().toLowerCase())));
  const entry = reports?.find((item) => item.id === selected);
  const title = entry?.name || report?.name || entry?.ticker || report?.ticker || "研报";
  return <div className="flex h-full min-h-0 flex-col">
    <header className="flex shrink-0 flex-wrap items-center gap-2 border-b bg-secondary px-3 py-2">
      <h1 className="mr-2 font-semibold">报告与知识库</h1>
      <button type="button" aria-pressed={tab === "reports"} onClick={() => setTab("reports")} className="rounded px-2 py-1 aria-pressed:bg-accent">研报</button>
      <button type="button" aria-pressed={tab === "knowledge"} onClick={() => setTab("knowledge")} className="rounded px-2 py-1 aria-pressed:bg-accent">知识库</button>
      <button type="button" onClick={refresh} aria-label="刷新报告与知识库" className="ml-auto rounded border p-1.5"><RefreshCw className="size-3.5" /></button>
      <button type="button" onClick={onNew} className="flex items-center gap-1 rounded bg-action px-2 py-1.5 text-gold-foreground"><Plus className="size-3.5" /> 新建研究</button>
    </header>
    {error && <p role="alert" className="border-b px-3 py-2 text-destructive">{error}</p>}
    <PanelGroup direction="horizontal" className="flex-1 min-h-0">
      <Panel defaultSize={35} minSize={23} className="min-w-0">
        <div className="h-full overflow-y-auto p-2.5">
          {tab === "reports" ? <>
            <div className="mb-2 flex flex-wrap gap-1.5">
              <input aria-label="筛选研报" className="min-w-[8rem] flex-1 rounded border bg-input px-2 py-1" placeholder="标的、代码或摘要" value={filter} onChange={(event) => setFilter(event.target.value)} />
              <select aria-label="研报模板" className="rounded border bg-input px-1.5" value={template} onChange={(event) => setTemplate(event.target.value)}>
                <option value="">全部模板</option>{TEMPLATES.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
              </select>
            </div>
            {!filtered && <Skeleton className="h-28" />}
            {filtered?.length === 0 && <p className="p-2 text-muted-foreground">没有符合条件的研报。</p>}
            {!!filtered?.length && <div className="overflow-auto rounded border">
              <table className="w-full text-left text-sm">
                <thead className="bg-secondary text-xs text-muted-foreground"><tr><th className="p-2">标的</th><th>模板</th><th className="pr-2 text-right">日期</th></tr></thead>
                <tbody>{filtered.map((item) => <tr key={item.id} className={(selected === item.id ? "bg-accent" : "hover:bg-secondary") + " border-t"}>
                  <td className="p-2"><button type="button" onClick={() => choose(item.id)} className="text-left text-gold">
                    {item.name || item.ticker}<span className="block font-mono text-xs text-muted-foreground">{item.ticker}</span>
                  </button></td>
                  <td className="text-xs">{labels.get(item.template) || item.template}</td>
                  <td className="whitespace-nowrap pr-2 text-right font-mono text-xs tabular-nums">{(item.as_of || item.created_at || "").slice(0, 10)}</td>
                </tr>)}</tbody>
              </table>
            </div>}
          </> : <>
            <div className="mb-2 flex items-center gap-1">
              <input aria-label="检索知识库" className="min-w-0 flex-1 rounded border bg-input px-2 py-1" placeholder="检索研报、公告和对话" value={query}
                onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !isImeKeyEvent(event.nativeEvent) && !searching) void search(query); }} />
              <button type="button" aria-label="检索" disabled={searching} className="rounded border p-1.5" onClick={() => void search(query)}><Search className="size-3.5" /></button>
            </div>
            {searching && <Skeleton className="mb-3 h-20" />}
            {hits && <div className="mb-3 divide-y rounded border">
              {!hits.length && <p className="p-2 text-muted-foreground">未检索到相关片段。</p>}
              {hits.map((item, index) => <button key={index} type="button" onClick={() => {
                if (item.report_id) choose(item.report_id);
                else { setSelected(""); setHit(item); }
              }} className="block w-full p-2 text-left hover:bg-accent">
                <span className="block truncate text-gold">{item.title || item.name || item.ticker || "本地知识片段"}</span>
                <span className="line-clamp-2 text-xs text-muted-foreground">{item.text}</span>
              </button>)}
            </div>}
            <label className="mb-2 flex cursor-pointer items-center gap-2 rounded border border-dashed bg-secondary p-3"
              onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file) upload(file); }}>
              <Upload className="size-4 shrink-0" />
              <span>{busy ? "正在处理…" : "拖入或选择 PDF、DOCX、MD、TXT"}</span>
              <input type="file" className="sr-only" accept=".pdf,.docx,.md,.txt" disabled={busy}
                onChange={(event) => { const file = event.target.files?.[0]; if (file) upload(file); event.target.value = ""; }} />
            </label>
            <div className="flex justify-between px-1 py-1.5"><h2 className="font-medium">已导入文件</h2>
              <button type="button" disabled={busy} onClick={() => void act(async () => { await api.knowledgeReindex(); refresh(); })} className="text-xs text-gold">重建研报索引</button>
            </div>
            {!documents && <Skeleton className="h-16" />}
            {documents?.length === 0 && <p className="p-2 text-muted-foreground">还没有导入文件。</p>}
            {documents?.map((doc) => <div className="flex items-center gap-2 border-b p-2" key={doc.id}>
              <FileText className="size-4 shrink-0 text-muted-foreground" /><div className="min-w-0 flex-1">
                <p className="truncate" title={doc.title}>{doc.title}</p><p className="text-xs text-muted-foreground">{doc.chunks} 个分块 {doc.ticker && "· " + doc.ticker}</p>
              </div>
              <button type="button" aria-label={"删除 " + doc.title} disabled={busy} onClick={async () => {
                if (await confirm("删除文档与索引？", doc.title)) await act(async () => { await api.knowledgeDeleteDocument(doc.id); setHit(undefined); setHits(undefined); refresh(); });
              }}><Trash2 className="size-3.5" /></button>
            </div>)}
          </>}
        </div>
      </Panel>
      <PanelResizeHandle className="w-1.5 border-x bg-secondary hover:bg-[var(--gold-muted)]" />
      <Panel minSize={42} className="min-w-0">
        <div className="flex h-full min-h-0 flex-col">
          {selected ? <>
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b p-2.5">
              <h2 className="mr-auto font-medium">{title}</h2>
              <button type="button" onClick={() => setReading("report")} aria-pressed={reading === "report"} className="rounded px-2 py-1 aria-pressed:bg-accent">研报正文</button>
              <button type="button" onClick={() => setReading("ledger")} aria-pressed={reading === "ledger"} className="rounded px-2 py-1 aria-pressed:bg-accent">事实对账单</button>
              <select aria-label="导出研报" value="" className="rounded border bg-input px-1.5 py-1"
                onChange={(event) => { const value = event.target.value; if (value === "poster") setPoster(true); else if (value) void act(() => downloadReportFile(selected, value as ReportFormat)); }} disabled={!report || busy}>
                <option value="">导出</option><option value="poster">微信高清长图</option>
                {FORMATS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
              <button type="button" aria-label="删除研报" disabled={busy} className="p-1 text-muted-foreground hover:text-destructive" onClick={async () => {
                if (await confirm("删除这份研报？", title)) await act(async () => { await api.deleteReport(selected); choose(""); refresh(); });
              }}><Trash2 className="size-3.5" /></button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto">
              {reading === "ledger" ? <ReportLedgerPanel id={selected} />
                : report ? <ReportPreview report={report} /> : detailError ? <p role="alert" className="p-4 text-destructive">{detailError}</p> : <Skeleton className="m-4 h-48" />}
            </div>
            {report && <SharePosterView open={poster} onOpenChange={setPoster} report={report} name={title} />}
          </> : hit ? <section className="overflow-auto p-4">
            <h2 className="mb-3 font-medium">{hit.title || "知识库片段"}</h2><StructuredText content={hit.text || ""} />
          </section> : <div className="grid flex-1 place-content-center text-center text-muted-foreground">
            <BookOpen className="mx-auto mb-2 size-5" /><p>选择研报或知识片段开始阅读。</p>
          </div>}
        </div>
      </Panel>
    </PanelGroup>
  </div>;
}
