import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import {
  Check, Loader2, ShieldAlert, Link2, Clock3, BrainCircuit, ChevronDown, FileDown, RefreshCw, Sparkles, Square, ExternalLink,
} from "lucide-react";
import { TEMPLATES, type TemplateId, type ReportDoc } from "../../data/report";
import { fmt, signed, trendClass } from "../../lib/format";
import { StructuredText } from "../../lib/markdown";
import { api, downloadReportFile, type ResearchRef } from "../../api/client";
import { useResearchStream } from "../../hooks/useResearchStream";
import { SharePosterView } from "./SharePosterView";

/**
 * v1.8.0：研究卡片 —— 研究任务以卡片形式嵌入「研究对话」。
 *
 * 由原 Workspace 抽出（事件处理与报告渲染原样迁移），按 task_id 连接研究 SSE：
 * - 运行中：工具轨迹 + 报告自上而下逐 token 流式（单次流式撰写：摘要 → 各章节首尾相接）；
 * - 已完成：后端终态回放直接给出完整报告（重开对话即可看到，无需另外取数）；
 * - 断线：hook 自动重连 3 次，耗尽后卡片提供「重新连接」。
 */

interface CurrentStock {
  code: string;
  name: string;
  price?: number;
  change?: number;
  changePct?: number;
}

interface ToolCall {
  name: string;
  label: string;
  status: "start" | "ok" | "error";
  events: { text: string; time: string }[];
}

/** 报告章节的流式中间态。queued：已排上；writing：撰写中；done：权威正文已收口。 */
export interface PartialSection {
  title: string;
  body: string;
  status?: "queued" | "writing" | "done";
  thinking?: string;
  thinkingChars?: number;
}

export const SECTION_ORDER: Record<TemplateId, string[]> = {
  market_recap: ["宽基指数", "成交与广度", "申万一级行业", "当日市场快讯"],
  equity_deep: ["技术面分析", "基本面分析", "估值分析", "行业与公司分析"],
  industry: ["行业景气与竞争格局"],
  earnings: ["业绩快评"],
  risk: ["风险情景与监测指标"],
};

const SUMMARY_LABEL: Record<TemplateId, string> = {
  market_recap: "收盘复盘摘要",
  equity_deep: "个股深度摘要",
  industry: "行业研究摘要",
  earnings: "财报点评摘要",
  risk: "风险评估摘要",
};

function templateOf(t: string | undefined): TemplateId {
  return (TEMPLATES.some((x) => x.id === t) ? t : "equity_deep") as TemplateId;
}

/** 按标题插入/更新章节，并按模板章节序排序（未知标题排末尾）。 */
function upsertSection(
  items: PartialSection[],
  title: string,
  template: TemplateId,
  update: (cur: PartialSection) => PartialSection,
): PartialSection[] {
  const idx = items.findIndex((item) => item.title === title);
  const next = items.slice();
  if (idx === -1) next.push(update({ title, body: "" }));
  else next[idx] = update(items[idx]);
  const order = SECTION_ORDER[template] ?? [];
  return next.sort((a, b) => {
    const ai = order.indexOf(a.title);
    const bi = order.indexOf(b.title);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
  });
}

const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

export function ResearchCard({
  research,
  initialStatus,
  initialReportId,
  initialError,
  onDone,
  onStatusChange,
  defaultTrailOpen = false,
}: {
  research: ResearchRef;
  initialStatus?: "pending" | "running" | "done" | "error";
  initialReportId?: string | null;
  initialError?: string | null;
  onDone?: (r: { id: string; name: string; code: string; template: string; date: string; summary: string }) => void;
  onStatusChange?: (phase: "running" | "done" | "error" | "disconnected") => void;
  defaultTrailOpen?: boolean;
}) {
  const template = templateOf(research.template);
  const isInitialDone = initialStatus === "done" && !!initialReportId;
  const isInitialError = initialStatus === "error";
  const [phase, setPhase] = useState<"running" | "done" | "error" | "disconnected">(
    isInitialDone ? "done" : isInitialError ? "error" : "running",
  );
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [doc, setDoc] = useState<ReportDoc | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [loadingSavedDoc, setLoadingSavedDoc] = useState<boolean>(isInitialDone);
  const [reportId, setReportId] = useState(initialReportId || "");
  const [stock, setStock] = useState<CurrentStock>({ code: research.code, name: research.name || research.code });
  const [typed, setTyped] = useState("");
  const [thinking, setThinking] = useState<{ text: string; chars: number } | null>(null);
  const [sections, setSections] = useState<PartialSection[]>([]);
  const [error, setError] = useState(isInitialError ? initialError || "研究任务未完成" : "");
  const [trailOpen, setTrailOpen] = useState(defaultTrailOpen);
  const [collapsed, setCollapsed] = useState(false);
  const [connectNonce, setConnectNonce] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [exportErr, setExportErr] = useState("");
  const stream = useResearchStream();
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  // 已完成的历史任务打开时不再重复触发 onDone（避免每次点历史任务都刷一遍报告库）
  const notifiedRef = useRef(isInitialDone);
  const mountedPhaseRef = useRef(false);

  useEffect(() => {
    if (!mountedPhaseRef.current) {
      mountedPhaseRef.current = true;
      return;
    }
    onStatusChangeRef.current?.(phase);
  }, [phase]);

  // 若 SSE 半开静默而外层轮询已观测到任务落库完成，主动触发一次终态重连以加载报告
  useEffect(() => {
    if ((phase === "running" || phase === "disconnected") && initialStatus === "done") {
      setConnectNonce((n) => n + 1);
    }
  }, [initialStatus, phase]);

  // 报告在「报告库」被真删除时同步更新本卡片，防止悬空下载或自动重连
  useEffect(() => {
    const onDeleted = (e: Event) => {
      const detail = (e as CustomEvent<{ id?: string; taskId?: string }>).detail;
      if (!detail) return;
      if ((detail.id && detail.id === reportId) || (detail.taskId && detail.taskId === research.task_id)) {
        stream.close();
        setDoc(null);
        setReportId("");
        setPhase("error");
        setError("该研究报告已从报告库删除。");
      }
    };
    window.addEventListener("finresearch:report-deleted", onDeleted);
    return () => window.removeEventListener("finresearch:report-deleted", onDeleted);
  }, [reportId, research.task_id, stream]);

  useEffect(() => {
    let finished = false;
    // 快速通道 1：历史已完成且已有 report_id → 直接走 REST 拉报告，不开 SSE 连接，杜绝「研究进行中」闪烁
    if (isInitialDone && initialReportId && connectNonce === 0) {
      setLoadingSavedDoc(true);
      api
        .report(initialReportId)
        .then((rep) => {
          if (finished) return;
          setDoc(rep);
          setReportId(initialReportId);
          setStock((cur) => ({ ...cur, code: rep?.ticker || cur.code, name: rep?.name || cur.name }));
          setPhase("done");
        })
        .catch((e) => {
          if (finished) return;
          setPhase("error");
          setError(`读取已归档报告失败：${e instanceof Error ? e.message : String(e)}`);
        })
        .finally(() => {
          if (!finished) setLoadingSavedDoc(false);
        });
      return () => {
        finished = true;
      };
    }

    // 快速通道 2：历史失败/已取消任务且用户未点重连 → 直接展示失败原因，不开无意义的 SSE
    if (isInitialError && connectNonce === 0) {
      setPhase("error");
      setError(initialError || "研究任务未完成");
      return () => {
        finished = true;
      };
    }

    const finish = () => {
      if (finished) return;
      finished = true;
      stream.close();
    };
    interface ReplayBuffer {
      toolCalls: ToolCall[];
      typed: string;
      thinking: { text: string; chars: number } | null;
      sections: PartialSection[];
      stockPatch: Partial<CurrentStock>;
    }
    let replayBuf: ReplayBuffer | null = null;

    const commitReplayIfActive = () => {
      if (!replayBuf) return;
      const snap = replayBuf;
      replayBuf = null;
      setToolCalls(snap.toolCalls);
      setTyped(snap.typed);
      setThinking(snap.thinking);
      setSections(snap.sections);
      if (Object.keys(snap.stockPatch).length > 0) {
        setStock((cur) => ({ ...cur, ...snap.stockPatch }));
      }
      setDoc(null);
      setError("");
      setPhase("running");
    };

    const applyToolCalls = (fn: (calls: ToolCall[]) => ToolCall[]) => {
      if (replayBuf) {
        replayBuf.toolCalls = fn(replayBuf.toolCalls);
      } else {
        setToolCalls(fn);
      }
    };
    const applySections = (fn: (items: PartialSection[]) => PartialSection[]) => {
      if (replayBuf) {
        replayBuf.sections = fn(replayBuf.sections);
      } else {
        setSections(fn);
      }
    };
    const settleOpenTools = (finalStatus: "ok" | "error") => {
      applyToolCalls((calls) =>
        calls.map((c) => (c.status === "start" ? { ...c, status: finalStatus } : c)),
      );
    };
    const emit = (text: string) =>
      applyToolCalls((calls) => {
        if (!calls.length) return [{ name: "_task", label: "研究任务", status: "start", events: [{ text, time: now() }] }];
        const last = calls[calls.length - 1];
        return [...calls.slice(0, -1), { ...last, events: [...last.events, { text, time: now() }] }];
      });

    stream.connect(research.task_id, research.code, template, (ev: any) => {
      if (finished) return;
      switch (ev.type) {
        case "replay":
          // 重连回放先在内存缓冲重建，待 replay_end / done / error 到达时一次性原子替换，杜绝重连白屏闪烁
          replayBuf = { toolCalls: [], typed: "", thinking: null, sections: [], stockPatch: {} };
          break;
        case "replay_end":
          commitReplayIfActive();
          break;
        case "sections_reset":
          if (replayBuf) {
            replayBuf.typed = "";
            replayBuf.thinking = null;
            replayBuf.sections = [];
          } else {
            setTyped("");
            setThinking(null);
            setSections([]);
          }
          break;
        case "section_reset": {
          const title = ev.title ?? "分析章节";
          applySections((items) => upsertSection(items, title, template, (cur) => (
            cur.status === "done" ? cur : { ...cur, body: "", thinking: undefined, thinkingChars: undefined }
          )));
          break;
        }
        case "tool_call":
          applyToolCalls((calls) => {
            if (ev.status === "start") {
              const placeholder = calls.length === 1 && calls[0].name === "_task" ? calls[0].events : [];
              const base = calls.filter((c) => c.name !== "_task");
              return [...base, { name: ev.name, label: ev.label || ev.name, status: "start", events: placeholder }];
            }
            for (let i = calls.length - 1; i >= 0; i--) {
              if (calls[i].name === ev.name && calls[i].status === "start") {
                const next = [...calls];
                next[i] = { ...next[i], status: ev.status };
                return next;
              }
            }
            return calls;
          });
          break;
        case "tool":
          emit(ev.text);
          break;
        case "summary":
          // 替换语义：单次流式下首帧只带标的头信息（text 为空），正文随后由 summary_token 流出
          if (replayBuf) {
            replayBuf.typed = ev.text ?? "";
            if (ev.name) replayBuf.stockPatch.name = ev.name;
            if (ev.ticker) replayBuf.stockPatch.code = ev.ticker;
            if (ev.price != null) replayBuf.stockPatch.price = ev.price;
            if (ev.change != null) replayBuf.stockPatch.change = ev.change;
            if (ev.changePct != null) replayBuf.stockPatch.changePct = ev.changePct;
          } else {
            setTyped(ev.text ?? "");
            if (ev.name || ev.ticker) {
              setStock((cur) => ({
                ...cur,
                name: ev.name || cur.name,
                code: ev.ticker || cur.code,
                price: ev.price ?? cur.price,
                change: ev.change ?? cur.change,
                changePct: ev.changePct ?? cur.changePct,
              }));
            }
          }
          break;
        case "summary_token":
        case "token":
          if (replayBuf) {
            replayBuf.typed += ev.text ?? "";
          } else {
            setTyped((t) => t + (ev.text ?? ""));
          }
          break;
        case "report_thinking":
          if (replayBuf) {
            replayBuf.thinking = { text: ev.text ?? "", chars: ev.chars ?? 0 };
          } else {
            setThinking({ text: ev.text ?? "", chars: ev.chars ?? 0 });
          }
          break;
        case "section_queued":
        case "section_start": {
          const title = ev.title ?? "分析章节";
          const status = ev.type === "section_start" ? "writing" : "queued";
          applySections((items) => upsertSection(items, title, template, (cur) => ({
            ...cur,
            status: cur.status === "done" ? "done" : cur.status === "writing" ? "writing" : status,
          })));
          break;
        }
        case "section_thinking": {
          const title = ev.title ?? "分析章节";
          applySections((items) => upsertSection(items, title, template, (cur) => (
            cur.status === "done" ? cur : { ...cur, status: "writing", thinking: ev.text ?? "", thinkingChars: ev.chars ?? 0 }
          )));
          break;
        }
        case "section_token": {
          const title = ev.title ?? "分析章节";
          const token = ev.text ?? "";
          applySections((items) => upsertSection(items, title, template, (cur) => (
            cur.status === "done" ? cur : { ...cur, status: "writing", body: cur.body + token }
          )));
          break;
        }
        case "section": {
          const title = ev.title ?? "分析章节";
          applySections((items) => upsertSection(items, title, template, () => ({ title, body: ev.body ?? "", status: "done" })));
          break;
        }
        case "done": {
          commitReplayIfActive();
          const rep = ev.report as ReportDoc & { ticker?: string; name?: string };
          settleOpenTools("ok");
          setDoc(rep);
          setReportId(ev.report_id || "");
          setStock((cur) => ({ ...cur, code: rep?.ticker || cur.code, name: rep?.name || cur.name }));
          setPhase("done");
          if (!notifiedRef.current) {
            notifiedRef.current = true;
            onDoneRef.current?.({
              id: ev.report_id || research.task_id,
              name: rep?.name || research.name || research.code,
              code: rep?.ticker || research.code,
              template: TEMPLATES.find((t) => t.id === template)!.label,
              date: rep?.as_of ?? new Date().toLocaleString("zh-CN", { hour12: false }),
              summary: rep?.summary ?? "",
            });
          }
          finish();
          break;
        }
        case "error":
          commitReplayIfActive();
          settleOpenTools("error");
          setError(ev.message ?? "研究管线失败");
          setPhase("error");
          finish();
          break;
        case "__disconnected__":
          replayBuf = null;
          settleOpenTools("error");
          setError("研究流连接中断（任务仍在后台运行），可点击「重新连接」恢复。");
          setPhase("disconnected");
          finish();
          break;
        default:
          break; // heartbeat 等
      }
    });
    return () => {
      finished = true;
      stream.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [research.task_id, connectNonce]);

  const running = phase === "running";
  const label = TEMPLATES.find((t) => t.id === template)!.label;
  const activeTool = [...toolCalls].reverse().find((c) => c.status === "start");
  const lastEvent = toolCalls.length ? toolCalls[toolCalls.length - 1].events.slice(-1)[0]?.text : "";

  return (
    <div className="w-full rounded-xl border border-gold/25 bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border bg-accent/30">
        <Sparkles className="size-4 text-gold shrink-0" />
        <span className="text-xs px-2 py-0.5 rounded bg-accent text-gold shrink-0">{label}</span>
        <span className="text-sm truncate">{stock.name}<span className="text-muted-foreground tabular ml-1 text-xs">{stock.code !== stock.name ? stock.code : ""}</span></span>
        <span className={`ml-auto flex items-center gap-1 text-[11px] shrink-0 ${phase === "done" ? "text-down" : phase === "error" ? "text-up" : "text-gold"}`}>
          {phase === "done" ? <Check className="size-3.5" /> : phase === "error" || phase === "disconnected" ? <ShieldAlert className="size-3.5" /> : <Loader2 className="size-3.5 animate-spin" />}
          {phase === "done" ? "已完成 · 已入知识库" : phase === "disconnected" ? "连接中断" : phase === "error" ? "未完成" : (activeTool?.label || "研究进行中")}
        </span>
        {running && (
          <button
            type="button"
            disabled={cancelling}
            onClick={() => {
              setCancelling(true);
              api.cancelResearch(research.task_id)
                .then((res) => {
                  if (res?.status === "done" || res?.status === "error") {
                    // 取消请求到达前任务已落库完成或已报错：重连终态回放以加载真实终态，避免把真实报错覆盖为「已取消」
                    stream.close();
                    setConnectNonce((n) => n + 1);
                    return;
                  }
                  stream.close();
                  setToolCalls((calls) =>
                    calls.map((c) => (c.status === "start" ? { ...c, status: "error" } : c)),
                  );
                  setPhase("error");
                  setError("研究任务已取消。");
                })
                .catch((e) => setError(`取消失败：${e instanceof Error ? e.message : String(e)}`))
                .finally(() => setCancelling(false));
            }}
            className="flex items-center gap-1 rounded px-2 py-0.5 text-[11px] bg-destructive/15 text-destructive hover:bg-destructive/25 disabled:opacity-50 shrink-0 transition-colors"
            title="取消当前研究任务"
          >
            <Square className="size-2.5 fill-current" /> 取消
          </button>
        )}
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="grid place-items-center size-6 rounded hover:bg-secondary text-muted-foreground shrink-0"
          title={collapsed ? "展开报告" : "收起报告"}
        >
          <ChevronDown className={`size-3.5 transition-transform ${collapsed ? "" : "rotate-180"}`} />
        </button>
      </div>

      {!collapsed && (
        <>
          {/* 执行轨迹（默认折叠为一行最新进度） */}
          {(toolCalls.length > 0 || running) && (
            <div className="px-4 py-2 border-b border-border/60 text-[11px] text-muted-foreground">
              <button onClick={() => setTrailOpen((v) => !v)} className="flex w-full items-center gap-2 text-left">
                <span className="flex items-center gap-1 shrink-0">
                  {toolCalls.map((c, i) => (
                    <span key={`${c.name}-${i}`} className={`size-1.5 rounded-full ${c.status === "ok" ? "bg-down" : c.status === "error" ? "bg-up" : "bg-gold animate-pulse"}`} />
                  ))}
                </span>
                <span className="truncate flex-1">{lastEvent || "正在连接研究引擎…"}</span>
                <ChevronDown className={`size-3 shrink-0 transition-transform ${trailOpen ? "rotate-180" : ""}`} />
              </button>
              <AnimatePresence>
                {trailOpen && (
                  <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
                    <div className="mt-2 space-y-1.5">
                      {toolCalls.map((call, ci) => (
                        <div key={`${call.name}-${ci}`}>
                          <div className="flex items-center gap-1.5 text-foreground/80">
                            {call.status === "ok" ? <Check className="size-3 text-down" /> : call.status === "error" ? <ShieldAlert className="size-3 text-up" /> : <Loader2 className="size-3 text-gold animate-spin" />}
                            {call.label}
                          </div>
                          {call.events.map((e, i) => (
                            <div key={i} className="pl-4.5 flex gap-2"><span className="font-mono text-[9px] opacity-60">{e.time}</span><span>{e.text}</span></div>
                          ))}
                        </div>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {error && (
            <div className="px-4 py-2 text-xs text-destructive flex items-center gap-2 border-b border-border/60">
              <span className="flex-1">{error}</span>
              {error.includes("重新连接") && (
                <button onClick={() => { setError(""); setPhase("running"); setConnectNonce((n) => n + 1); }} className="flex items-center gap-1 px-2 py-1 rounded bg-secondary text-foreground/80 hover:bg-secondary/70">
                  <RefreshCw className="size-3" /> 重新连接
                </button>
              )}
            </div>
          )}

          {loadingSavedDoc && !doc && (
            <div className="px-5 py-10 flex items-center justify-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-4 animate-spin text-gold" /> 正在加载已归档研究报告…
            </div>
          )}

          {(doc || typed || sections.length > 0 || (running && thinking) || running) && (
            <ResearchReportView
              doc={doc}
              stock={stock}
              summary={doc?.summary || typed}
              thinking={thinking}
              sections={doc ? (doc.sections || []).map((s) => ({ ...s, status: "done" as const })) : sections}
              template={template}
              running={running}
              incomplete={phase === "error" || phase === "disconnected"}
            />
          )}

          {phase === "done" && reportId && (
            <div className="flex flex-wrap items-center gap-1.5 px-4 pb-3 -mt-2">
              <span className="text-[11px] text-muted-foreground mr-1">导出</span>
              {doc && <button type="button" className="rounded bg-action px-2 py-1 text-xs text-gold-foreground" onClick={() => setShareOpen(true)}>微信高清长图</button>}
              {(["docx", "pdf", "txt", "md"] as const).map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => {
                    setExportErr("");
                    downloadReportFile(reportId, f).catch((e) => setExportErr(e instanceof Error ? e.message : String(e)));
                  }}
                  className="flex items-center gap-1 text-[11px] px-2 py-1 rounded bg-secondary hover:bg-secondary/70 text-foreground/80 transition-colors"
                >
                  <FileDown className="size-3" /> {f.toUpperCase()}
                </button>
              ))}
              {exportErr && <span className="text-[11px] text-destructive ml-2">{exportErr}</span>}
              {doc && <SharePosterView open={shareOpen} onOpenChange={setShareOpen} report={doc} name={stock.name} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function ResearchReportView({ doc, stock, summary, thinking, sections, template, running, incomplete = false }: {
  doc: ReportDoc | null;
  stock: CurrentStock;
  summary: string;
  thinking?: { text: string; chars: number } | null;
  sections: PartialSection[];
  template: TemplateId;
  running: boolean;
  incomplete?: boolean;
}) {
  const expected = SECTION_ORDER[template] ?? [];
  const finYears = doc?.financials?.years ?? [];
  const finRows = doc?.financials?.rows ?? [];
  const riskItems = doc?.risks ?? [];
  const sourceItems = doc?.sources ?? [];
  const visibleSections: (PartialSection & { pending: boolean })[] = running
    ? [
        ...expected.map((title) => {
          const hit = sections.find((item) => item.title === title);
          return hit ? { ...hit, pending: false } : { title, body: "", pending: true };
        }),
        ...sections.filter((item) => !expected.includes(item.title)).map((item) => ({ ...item, pending: false })),
      ]
    : sections.map((item) => ({ ...item, pending: false }));
  const anySectionStarted = sections.some((s) => s.status === "writing" || s.status === "done");
  const summaryStreaming = running && !anySectionStarted;
  const observationLabel = template === "risk" ? "监测重点" : template === "earnings" ? "后续观察" : template === "industry" ? "行业催化" : "催化剂";

  return (
    <div className="px-5 py-4 max-w-3xl">
      <div className="border-b border-gold/30 pb-3">
        <h2 className="font-display" style={{ fontSize: 20, fontWeight: 700 }}>
          {stock.name} <span className="text-muted-foreground tabular" style={{ fontSize: 14 }}>({stock.code})</span>
        </h2>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-1.5 text-xs text-muted-foreground">
          <span>数据时点：{doc?.as_of || (running ? "生成中" : "—")}</span>
          {stock.price != null && Number.isFinite(stock.price) && stock.price > 0 && (
            <span className={trendClass(stock.change ?? stock.changePct ?? 0)}>
              现价 {fmt(stock.price)} ({stock.changePct == null ? "—" : `${signed(stock.changePct)}%`})
            </span>
          )}
          <span className={running ? "text-gold" : "text-down"}>{running ? "一次性流式撰写中" : "数字零幻觉 · 可溯源"}</span>
        </div>
        {doc?.llm_degraded && (
          <div className="mt-2 rounded-md border border-gold/30 bg-gold/10 px-2.5 py-1.5 text-[11px] text-gold">
            提示：本次生成过程中模型服务不可用或触发限流，部分内容已由本地规则兜底生成。
          </div>
        )}
      </div>

      <div className="mt-4 rounded-lg border border-gold/20 bg-accent/40 p-3.5">
        <div className="text-xs text-gold mb-1.5">{SUMMARY_LABEL[template]}</div>
        {summary ? (
          <div className="text-sm leading-relaxed text-foreground/90">
            <StructuredText content={summary} streaming={summaryStreaming} citations={sourceItems} />
          </div>
        ) : running && thinking ? (
          <div className="text-xs text-muted-foreground">
            <div className="flex items-center gap-2">
              <BrainCircuit className="size-3.5 text-gold animate-pulse" />
              模型思考中{thinking.chars ? `（已思考 ${thinking.chars} 字）` : ""}，思考完成后从摘要开始一次性写到最后一节…
            </div>
            {thinking.text && <p className="mt-1.5 pl-5 italic text-muted-foreground/60 line-clamp-2 break-all">…{thinking.text}</p>}
          </div>
        ) : running ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin text-gold" /> 正在取数并准备撰写…
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">摘要暂不可用。</p>
        )}
        {doc?.catalyst && (
          <p className="text-xs text-muted-foreground mt-2"><span className="text-foreground/70">{observationLabel}：</span>{doc.catalyst}</p>
        )}
      </div>

      {doc && finYears.length > 0 && finRows.length > 0 && (
        <div className="mt-5">
          <h3 className="font-display border-l-[3px] border-gold pl-2.5 mb-2.5" style={{ fontSize: 15, fontWeight: 600 }}>财务分析</h3>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-secondary/50">
                  <th className="text-left px-3 py-2 text-muted-foreground font-normal">指标</th>
                  {finYears.map((y) => <th key={y} className="px-3 py-2 text-muted-foreground font-normal tabular">{y}</th>)}
                </tr>
              </thead>
              <tbody>
                {finRows.map((r, i) => (
                  <tr key={r.name} className={i % 2 ? "bg-secondary/20" : ""}>
                    <td className="text-left px-3 py-2 text-foreground/80">{r.name}</td>
                    {(r.values || []).map((v, j) => <td key={j} className="px-3 py-2 text-center tabular text-foreground/90">{v}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {visibleSections.map((sec) => (
        <div key={sec.title} className="mt-5">
          <h3 className="font-display border-l-[3px] border-gold pl-2.5 mb-1.5" style={{ fontSize: 15, fontWeight: 600 }}>{sec.title}</h3>
          {sec.pending ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground/70">
              <Clock3 className="size-3.5" /> 等待前序内容写完
            </div>
          ) : running && sec.status === "queued" && !(sec.body || "").trim() ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground/70">
              <Clock3 className="size-3.5" /> 排队中 · 前序章节写完后紧接着开始
            </div>
          ) : (sec.body || "").trim() ? (
            <div className="text-sm leading-relaxed text-foreground/80">
              <StructuredText
                content={sec.body}
                streaming={running && sec.status === "writing"}
                citations={sourceItems}
              />
            </div>
          ) : running && sec.thinking ? (
            <div className="text-xs text-muted-foreground">
              <div className="flex items-center gap-2">
                <BrainCircuit className="size-3.5 text-gold animate-pulse" />
                模型思考中{sec.thinkingChars ? `（已思考 ${sec.thinkingChars} 字）` : ""}…
              </div>
              <p className="mt-1.5 pl-5 italic text-muted-foreground/60 line-clamp-2 break-all">…{sec.thinking}</p>
            </div>
          ) : running ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin text-gold" /> 正在生成本章节…
            </div>
          ) : incomplete ? (
            <p className="text-xs text-muted-foreground">任务未完成，本章节未生成。</p>
          ) : (
            <p className="text-xs text-muted-foreground">本章节暂无可展示内容（数据不足或撰写失败）。</p>
          )}
        </div>
      ))}

      {doc && riskItems.length > 0 && (
        <div className="mt-5">
          <h3 className="font-display border-l-[3px] border-up pl-2.5 mb-1.5 flex items-center gap-2" style={{ fontSize: 15, fontWeight: 600 }}>
            <ShieldAlert className="size-4 text-up" /> 风险提示
          </h3>
          <ul className="space-y-1">
            {riskItems.map((r, idx) => (
              <li key={`${idx}-${r}`} className="flex items-start gap-2 text-sm text-foreground/80"><span className="text-up mt-0.5">·</span> {r}</li>
            ))}
          </ul>
        </div>
      )}

      {doc && sourceItems.length > 0 && (
        <div className="mt-5">
          <h3 className="font-display border-l-[3px] border-border pl-2.5 mb-1.5" style={{ fontSize: 15, fontWeight: 600 }}>引用来源</h3>
          <ol className="space-y-1">
            {sourceItems.map((s, i) => {
              const extUrl = /^https?:\/\//i.test(s.url || "") ? s.url : "";
              return (
                <li key={i} className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Link2 className="size-3 shrink-0" />
                  <span className="text-foreground/70">[{i + 1}]</span>
                  {extUrl ? (
                    <a
                      href={extUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="truncate hover:text-gold hover:underline inline-flex items-center gap-1"
                    >
                      <span className="truncate">{s.title}</span>
                      <ExternalLink className="size-2.5 shrink-0 opacity-70" />
                    </a>
                  ) : (
                    <span className="truncate">{s.title}</span>
                  )}
                  <span className="font-mono text-[10px] text-muted-foreground/60 shrink-0">· {s.date}</span>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {doc?.disclaimer && <div className="mt-5 pt-3 border-t border-border text-[11px] leading-relaxed text-muted-foreground/70">{doc.disclaimer}</div>}
    </div>
  );
}
