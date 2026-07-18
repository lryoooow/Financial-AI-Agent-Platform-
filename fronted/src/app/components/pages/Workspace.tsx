import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Search, Sparkles, Check, Loader2, Circle, FileText, ShieldAlert, Link2, ArrowRight, Waypoints, Globe2 } from "lucide-react";
import { WATCHLIST } from "../../data/market";
import { TEMPLATES, type TemplateId, type ReportDoc } from "../../data/report";
import { fmt, signed, trendClass } from "../../lib/format";
import { api, type EvidenceStatus, type LlmConfig, type ResolveHit } from "../../api/client";
import { ModelPicker } from "../ModelPicker";

interface ActiveModel { provider: string; model: string; accent: string; }
interface Props {
  initialCode?: string;
  selectedCode?: string;
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onReportGenerated: (r: { id: string; name: string; code: string; template: string; date: string; summary: string }) => void;
}

interface CurrentStock {
  code: string;
  name: string;
  price?: number;
  change?: number;
  changePct?: number;
}

/** 一次真实的工具调用。后端 tool_call 事件驱动，不再是写死的阶段表。
 *
 * 旧的 STAGES 是照着后端固定管线的五个节点手写的，还给每格声明了 tools: [...]——
 * 但后端从不发工具事件，那些字段一直是装饰。现在后端换成 agent，
 * 会发真实的 tool_call（start → ok/error），这里就直接照实渲染。 */
interface ToolCall {
  name: string;
  label: string;
  status: "start" | "ok" | "error";
  events: { text: string; time: string }[];
}

/** agent 的待办快照（write_todos）。当前模板化研究只有取数+撰写两步，不需要待办；
 * 开放式研究落地后会有内容，届时无需改前端。 */
interface Todo { content: string; status: string; }

const SECTION_ORDER: Record<TemplateId, string[]> = {
  equity_deep: ["技术面分析", "基本面分析", "估值分析", "行业与公司分析"],
  industry: ["行业景气与竞争格局"],
  earnings: ["业绩快评"],
  risk: ["风险情景与监测指标"],
};

const SUMMARY_LABEL: Record<TemplateId, string> = {
  equity_deep: "个股深度摘要",
  industry: "行业研究摘要",
  earnings: "财报点评摘要",
  risk: "风险评估摘要",
};

export function Workspace({ initialCode, selectedCode, activeModel, onGoSettings, onReportGenerated }: Props) {
  const [query, setQuery] = useState(selectedCode ?? "");
  const [template, setTemplate] = useState<TemplateId>("equity_deep");
  const [displayTemplate, setDisplayTemplate] = useState<TemplateId>("equity_deep");
  const [phase, setPhase] = useState<"idle" | "running" | "done">("idle");
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [doc, setDoc] = useState<ReportDoc | null>(null);
  const [stock, setStock] = useState<CurrentStock>({ code: "600519.SH", name: "600519.SH" });
  const [typed, setTyped] = useState("");
  const [partialSections, setPartialSections] = useState<{ title: string; body: string }[]>([]);
  const [error, setError] = useState("");
  const [evidence, setEvidence] = useState<EvidenceStatus | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [llm, setLlm] = useState<LlmConfig | null>(null);
  const runId = useRef(0);
  const reportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (initialCode) {
      const code = initialCode.split(":")[0];
      setQuery(code);
      run(code);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCode]);

  useEffect(() => {
    if (!selectedCode || phase === "running") return;
    setQuery(selectedCode);
    setStock((s) => (
      s.code === selectedCode
        ? s
        : { code: selectedCode, name: selectedCode }
    ));
    // 只响应外部标的真正变化。旧实现依赖 phase，任务从 running→done 时会再次执行，
    // 用行情页旧标的覆盖刚完成报告的 ticker/name。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCode]);

  useEffect(() => {
    let cancelled = false;
    api.evidenceStatus()
      .then((d) => {
        if (!cancelled) {
          setEvidence(d);
          setEvidenceError("");
        }
      })
      .catch((e) => {
        if (!cancelled) setEvidenceError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

  function normalizeCode(input: string): string | null {
    const raw = input.trim().toUpperCase();
    const m = raw.match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
    if (!m) return null;
    const pure = m[1];
    if (m[2]) return `${pure}.${m[2]}`;
    if (/^(60|68|90|11|51|56)/.test(pure)) return `${pure}.SH`;
    if (/^(43|83|87|88)/.test(pure)) return `${pure}.BJ`;
    return `${pure}.SZ`;
  }

  async function resolveQuery(q: string, forTemplate: TemplateId): Promise<CurrentStock | null> {
    // 行业研究模板：输入即行业名（"光伏"/"半导体"），不做个股解析——
    // 否则会拿行业名去 api.resolve 当个股查，查不到就误报"未找到标的"。
    // 后端研究 agent 会据 template=industry 走 prefetch_industry 分支。
    if (forTemplate === "industry") {
      const name = q.trim();
      return name ? { code: name, name } : null;
    }
    const codeLike = normalizeCode(q);
    if (codeLike) {
      // 代码格式已确定时直接启动管线；行情节点会统一获取真实报价，避免同一次研究前后重复拉取。
      return { code: codeLike, name: codeLike };
    }
    const hits: ResolveHit[] = await api.resolve(q, 1);
    if (!hits.length) return null;
    return hits[0];
  }

  // —— 后端真实流程（SSE）：失败直接展示错误，禁止回退 mock ——
  async function runBackend(resolved: CurrentStock): Promise<boolean> {
    const id = ++runId.current;
    const runTemplate = template;
    setDisplayTemplate(runTemplate);
    setStock(resolved);
    setPhase("running");
    setDoc(null);
    setTyped("");
    setPartialSections([]);
    setError("");
    setToolCalls([]);
    setTodos([]);
    /** 进度文字挂到"当前正在跑的那个工具"下面；还没有工具时先放一条占位，不丢事件。 */
    const emit = (text: string) =>
      setToolCalls((calls) => {
        if (!calls.length) {
          return [{ name: "_task", label: "研究任务", status: "start", events: [{ text, time: now() }] }];
        }
        const last = calls[calls.length - 1];
        return [...calls.slice(0, -1), { ...last, events: [...last.events, { text, time: now() }] }];
      });

    let taskId: string;
    try {
      const r = await api.createResearch(resolved.code, runTemplate, undefined, llm ?? undefined);
      taskId = r.task_id;
    } catch {
      setError("无法提交研究任务：后端服务不可用或真实数据接口异常。");
      setPhase("idle");
      return false;
    }
    if (runId.current !== id) return true;

    emit(`提交研究任务 ${taskId.slice(0, 8)}…`);
    await new Promise<void>((resolve) => {
      const es = new EventSource(api.researchStreamUrl(taskId, resolved.code, runTemplate));
      const finish = () => { try { es.close(); } catch {} resolve(); };
      es.onmessage = (e: MessageEvent) => {
        if (runId.current !== id) { finish(); return; }
        let ev: any;
        try { ev = JSON.parse(e.data); } catch { return; }
        if (ev.type === "tool_call") {
          // agent 真实工具调用：start 追加一条，ok/error 更新最后那条同名的状态。
          setToolCalls((calls) => {
            if (ev.status === "start") {
              const placeholder = calls.length === 1 && calls[0].name === "_task" ? calls[0].events : [];
              const base = calls.filter((c) => c.name !== "_task");
              return [...base, { name: ev.name, label: ev.label || ev.name, status: "start", events: placeholder }];
            }
            for (let i = calls.length - 1; i >= 0; i--) {
              if (calls[i].name === ev.name) {
                const next = [...calls];
                next[i] = { ...next[i], status: ev.status };
                return next;
              }
            }
            return calls;
          });
        } else if (ev.type === "todo") {
          setTodos(ev.items ?? []);
        } else if (ev.type === "tool") {
          emit(ev.text);
        } else if (ev.type === "summary") {
          setTyped(ev.text ?? "");
          if (ev.name || ev.ticker) {
            setStock((current) => ({
              ...current,
              name: ev.name || current.name,
              code: ev.ticker || current.code,
              price: ev.price ?? current.price,
              change: ev.change ?? current.change,
              changePct: ev.changePct ?? current.changePct,
            }));
          }
        } else if (ev.type === "token") {
          // 兼容旧后端；新后端使用 summary 快照，重连时不会重复 append。
          setTyped((t) => t + (ev.text ?? ""));
        } else if (ev.type === "section_start") {
          // 真流式：某板块开始撰写，建一个空 body 的段并按模板序插入（不重复建段）
          const title = ev.title ?? "分析章节";
          setPartialSections((items) =>
            items.some((item) => item.title === title)
              ? items
              : [...items, { title, body: "" }].sort((a, b) => {
                  const order = SECTION_ORDER[runTemplate];
                  const ai = order.indexOf(a.title);
                  const bi = order.indexOf(b.title);
                  return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
                })
          );
        } else if (ev.type === "section_token") {
          // 真流式：逐 token 追加到对应板块 body（打字机效果）
          const title = ev.title ?? "分析章节";
          const token = ev.text ?? "";
          setPartialSections((items) => {
            const idx = items.findIndex((item) => item.title === title);
            if (idx === -1) return [...items, { title, body: token }];
            const next = items.slice();
            next[idx] = { title, body: next[idx].body + token };
            return next;
          });
        } else if (ev.type === "section") {
          // 权威收口：用最终 body 覆盖流式累积（截断/禁用词回退后以此为准）
          const section = { title: ev.title ?? "分析章节", body: ev.body ?? "" };
          setPartialSections((items) => [
            ...items.filter((item) => item.title !== section.title),
            section,
          ].sort((a, b) => {
            const order = SECTION_ORDER[runTemplate];
            const ai = order.indexOf(a.title);
            const bi = order.indexOf(b.title);
            return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
          }));
        } else if (ev.type === "done") {
          const rep = ev.report;
          const reportId = ev.report_id || `${resolved.code}-${Date.now()}`;
          setDoc(rep);
          setStock((current) => ({
            ...current,
            code: rep?.ticker || current.code,
            name: rep?.name || current.name,
          }));
          setPhase("done");
          reportRef.current?.scrollTo({ top: 0 });
          onReportGenerated({ id: reportId, name: rep?.name || resolved.name, code: rep?.ticker || resolved.code, template: TEMPLATES.find((t) => t.id === runTemplate)!.label, date: rep?.as_of ?? new Date().toLocaleString("zh-CN", { hour12: false }), summary: rep?.summary ?? "" });
          finish();
        } else if (ev.type === "error") {
          const message = ev.message ?? "研究管线失败";
          emit(`⚠ ${message}`);
          setError(message);
          setPhase("idle");
          finish();
        }
      };
      es.onerror = () => {
        if (runId.current === id) {
          setError("研究流连接中断，请检查后端服务或模型响应。");
          setPhase("idle");
        }
        finish();
      };
    });
    return true;
  }

  async function run(qOverride?: string) {
    const raw = qOverride ?? query;
    const q = (raw.trim() || selectedCode || "").trim();
    if (!q) {
      setError("请先选择或输入 A 股标的。");
      return;
    }
    setError("");
    let resolved: CurrentStock | null = null;
    try {
      resolved = await resolveQuery(q, template);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    if (!resolved) {
      setError(template === "industry" ? `请输入行业名称，如「光伏」` : `未找到真实标的：${q}`);
      return;
    }
    await runBackend(resolved);
  }

  const running = phase === "running";

  return (
    <div className="h-full min-h-0 grid grid-cols-1 xl:grid-cols-[380px_1fr] gap-4 p-5 overflow-hidden">
      {/* 左：输入 + 管线 */}
      <div className="flex flex-col gap-4 min-h-0 overflow-y-auto pr-1">
        <div className="rounded-xl border border-border bg-card p-4 shrink-0">
          <div className="flex items-center justify-between mb-3 gap-2">
            <h2 className="text-sm">研究任务</h2>
            <ModelPicker active={activeModel} onChange={setLlm} onGoSettings={onGoSettings} />
          </div>

          <div className="flex items-center gap-2 rounded-lg bg-input px-3 py-2.5 border border-transparent focus-within:border-gold/40 transition-colors">
            <Search className="size-4 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              placeholder={template === "industry" ? "输入行业，如 光伏 / 半导体 / 白酒" : "输入标的，如 贵州茅台 / 600519"}
              className="bg-transparent outline-none text-sm w-full placeholder:text-muted-foreground"
            />
          </div>
          {error && <div className="mt-2 text-xs text-destructive leading-relaxed">{error}</div>}

          <div className="mt-3 rounded-lg border border-border bg-secondary/30 px-3 py-2">
            <div className="flex items-center justify-between gap-2 text-[11px]">
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Globe2 className="size-3.5" />
                联网证据
              </span>
              {evidence ? (
                <span className={evidence.search.enabled ? "text-down" : "text-muted-foreground"}>
                  {evidence.search.enabled ? "Serper 已启用" : "国内源优先"}
                </span>
              ) : evidenceError ? (
                <span className="text-up">证据状态不可用</span>
              ) : (
                <span className="text-muted-foreground">读取中</span>
              )}
            </div>
            <p className="mt-1 text-[10.5px] leading-relaxed text-muted-foreground/80">
              {evidence
                ? evidence.summary
                : evidenceError
                  ? `后端未返回证据状态：${evidenceError}`
                  : "正在读取国内新闻/行情证据源与搜索配置。"}
            </p>
          </div>

          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {WATCHLIST.slice(0, 5).map((s) => (
              <button key={s.code} onClick={() => { setQuery(s.name); }} className="text-[11px] px-2 py-1 rounded-md bg-secondary/60 hover:bg-secondary text-muted-foreground hover:text-foreground transition-colors">
                {s.name}
              </button>
            ))}
          </div>

          <p className="text-[10px] tracking-[0.16em] text-muted-foreground uppercase mt-4 mb-2">报告模板</p>
          <div className="grid grid-cols-2 gap-2">
            {TEMPLATES.map((t) => (
              <button key={t.id} disabled={running} onClick={() => setTemplate(t.id)} className={`text-left p-2.5 rounded-lg border transition-all disabled:cursor-not-allowed disabled:opacity-60 ${template === t.id ? "border-gold/50 bg-accent" : "border-border hover:border-border/80 bg-secondary/30"}`}>
                <div className={`text-xs ${template === t.id ? "text-gold" : "text-foreground/90"}`}>{t.label}</div>
                <div className="text-[10px] text-muted-foreground mt-0.5 leading-tight">{t.desc}</div>
              </button>
            ))}
          </div>

          <button
            onClick={() => run()}
            disabled={running}
            className="mt-4 w-full flex items-center justify-center gap-2 rounded-lg bg-gold py-2.5 text-sm text-[#17140a] hover:brightness-110 disabled:opacity-60 disabled:cursor-not-allowed transition-all"
          >
            {running ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" strokeWidth={2.4} />}
            {running ? "研究进行中…" : "生成研究报告"}
          </button>
        </div>

        {/* 管线 */}
        <div className="rounded-xl border border-border bg-card p-4 flex-1 min-h-[220px] overflow-y-auto">
          <div className="flex items-center gap-2 mb-4">
            <Waypoints className="size-4 text-gold" />
            <span className="text-sm">智能体执行轨迹</span>
          </div>
          {phase === "idle" ? (
            <div className="text-xs text-muted-foreground leading-relaxed py-8 text-center">
              智能体自主编排：按需取数、逐段撰写
              <div className="mt-3 flex items-center justify-center gap-1.5 flex-wrap">
                <span className="px-2 py-1 rounded-md bg-secondary text-foreground/70 text-[10px]">取数</span>
                <ArrowRight className="size-3 text-muted-foreground/50" />
                <span className="px-2 py-1 rounded-md bg-secondary text-foreground/70 text-[10px]">撰写报告</span>
              </div>
              <div className="mt-2 text-[10px] text-muted-foreground/60">所有数值由本地工具计算，模型只引用</div>
            </div>
          ) : (
            <div className="space-y-1">
              {/* agent 待办：模板化研究只有两步用不到；开放式研究会有内容 */}
              {todos.length > 0 && (
                <div className="mb-3 rounded-lg bg-secondary/40 p-2.5">
                  <div className="text-[10px] text-muted-foreground mb-1.5">任务清单</div>
                  {todos.map((t, i) => (
                    <div key={i} className="flex items-start gap-2 text-[11px] leading-snug">
                      {t.status === "completed" ? (
                        <Check className="size-3 text-down mt-0.5 shrink-0" strokeWidth={3} />
                      ) : t.status === "in_progress" ? (
                        <Loader2 className="size-3 text-gold animate-spin mt-0.5 shrink-0" />
                      ) : (
                        <Circle className="size-3 text-border mt-0.5 shrink-0" strokeWidth={1.5} />
                      )}
                      <span className={t.status === "completed" ? "text-muted-foreground line-through" : "text-foreground/80"}>{t.content}</span>
                    </div>
                  ))}
                </div>
              )}
              {toolCalls.map((call, ci) => (
                <div key={`${call.name}-${ci}`} className="relative pl-7 pb-3">
                  {ci < toolCalls.length - 1 && <div className="absolute left-[9px] top-6 bottom-0 w-px bg-border" />}
                  <div className="absolute left-0 top-0.5">
                    {call.status === "ok" ? (
                      <motion.div initial={{ scale: 0 }} animate={{ scale: 1 }} className="grid place-items-center size-[19px] rounded-full bg-down/15">
                        <Check className="size-3 text-down" strokeWidth={3} />
                      </motion.div>
                    ) : call.status === "error" ? (
                      <div className="grid place-items-center size-[19px] rounded-full bg-up/15">
                        <ShieldAlert className="size-3 text-up" />
                      </div>
                    ) : (
                      <div className="grid place-items-center size-[19px] rounded-full bg-gold/15">
                        <Loader2 className="size-3.5 text-gold animate-spin" />
                      </div>
                    )}
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm text-foreground">{call.label}</span>
                    {call.name !== "_task" && (
                      <span className="text-[10px] text-muted-foreground/60 font-mono">{call.name}</span>
                    )}
                  </div>
                  <AnimatePresence>
                    {call.events.map((ev, i) => (
                      <motion.div key={i} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} className="flex items-start gap-2 mt-1.5 text-[11px] text-muted-foreground">
                        <span className="font-mono text-[9px] text-muted-foreground/50 mt-0.5">{ev.time}</span>
                        <span className="leading-snug">{ev.text}</span>
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 右：报告 */}
      <div ref={reportRef} className="rounded-xl border border-border bg-card min-h-0 overflow-y-auto">
        {!doc && phase !== "running" && (
          <div className="h-full grid place-items-center p-8 text-center">
            <div>
              <div className="mx-auto grid place-items-center size-14 rounded-2xl bg-secondary mb-4">
                <FileText className="size-6 text-muted-foreground" />
              </div>
              <h2 className="font-display" style={{ fontSize: 20, fontWeight: 600 }}>开始一次研究</h2>
              <p className="text-sm text-muted-foreground mt-2 max-w-sm">输入 A 股标的并选择模板，AI 将拉取行情、财务与新闻，生成带来源与免责声明的结构化报告。</p>
            </div>
          </div>
        )}
        {!doc && phase === "running" && !typed && partialSections.length === 0 && (
          <div className="h-full grid place-items-center p-8">
            <div className="flex flex-col items-center gap-3 text-muted-foreground">
              <Loader2 className="size-6 animate-spin text-gold" />
              <span className="text-sm">正在生成 {stock.name} 的研究报告…</span>
            </div>
          </div>
        )}
        {(doc || typed || partialSections.length > 0) && (
          <ResearchReportView
            doc={doc}
            stock={stock}
            summary={doc?.summary || typed}
            sections={doc?.sections || partialSections}
            template={displayTemplate}
            running={phase === "running"}
          />
        )}
      </div>
    </div>
  );
}

function ResearchReportView({ doc, stock, summary, sections, template, running }: {
  doc: ReportDoc | null;
  stock: CurrentStock;
  summary: string;
  sections: { title: string; body: string }[];
  template: TemplateId;
  running: boolean;
}) {
  const stagger = (i: number) => ({ initial: { opacity: 0, y: 14 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.08 * i, duration: 0.5, ease: [0.16, 1, 0.3, 1] as any } });
  const expected = SECTION_ORDER[template];
  const visibleSections = running
    ? [
        ...expected.map((title) => ({ title, body: sections.find((item) => item.title === title)?.body || "", pending: !sections.some((item) => item.title === title) })),
        ...sections.filter((item) => !expected.includes(item.title)).map((item) => ({ ...item, pending: false })),
      ]
    : sections.map((item) => ({ ...item, pending: false }));
  const observationLabel = template === "risk" ? "监测重点" : template === "earnings" ? "后续观察" : template === "industry" ? "行业催化" : "催化剂";
  return (
    <div className="p-6 max-w-3xl mx-auto">
      <motion.div {...stagger(0)} className="border-b border-gold/30 pb-4">
        <div className="flex items-center gap-2 text-[11px] text-gold mb-1.5">
          <span className="px-2 py-0.5 rounded bg-accent">{TEMPLATES.find((t) => t.id === template)!.label}</span>
          <span className={`flex items-center gap-1 ${running ? "text-gold" : "text-down"}`}>
            {running ? <Loader2 className="size-3 animate-spin" /> : <Check className="size-3" />}
            {running ? "分章节生成中" : "数字零幻觉 · 可溯源"}
          </span>
        </div>
        <h1 className="font-display" style={{ fontSize: 26, fontWeight: 700 }}>
          {stock.name} <span className="text-muted-foreground tabular" style={{ fontSize: 16 }}>({stock.code})</span>
        </h1>
        <div className="flex items-center gap-4 mt-2 text-xs text-muted-foreground">
          <span>报告日期：{doc?.as_of || new Date().toLocaleDateString("zh-CN")}</span>
          <span>数据时点：{doc?.as_of || "生成中"}</span>
          <span className={trendClass(stock.change ?? stock.changePct ?? 0)}>现价 {stock.price == null ? "—" : fmt(stock.price)} ({stock.changePct == null ? "—" : `${signed(stock.changePct)}%`})</span>
        </div>
      </motion.div>

      <motion.div {...stagger(1)} className="mt-5 rounded-lg border border-gold/20 bg-accent/40 p-4">
        <div className="text-xs text-gold mb-1.5">{SUMMARY_LABEL[template]}</div>
        <p className="text-sm leading-relaxed text-foreground/90">{summary}</p>
        {doc?.catalyst && <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-xs text-muted-foreground mt-2">
          <span className="text-foreground/70">{observationLabel}：</span>{doc.catalyst}
        </motion.p>}
      </motion.div>

      {doc && doc.financials.years.length > 0 && <motion.div {...stagger(2)} className="mt-6">
        <h2 className="font-display border-l-[3px] border-gold pl-2.5 mb-3" style={{ fontSize: 16, fontWeight: 600 }}>财务分析（近5年）</h2>
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-secondary/50">
                <th className="text-left px-3 py-2 text-muted-foreground font-normal">指标</th>
                {doc.financials.years.map((y) => <th key={y} className="px-3 py-2 text-muted-foreground font-normal tabular">{y}</th>)}
              </tr>
            </thead>
            <tbody>
              {doc.financials.rows.map((r, i) => (
                <tr key={r.name} className={i % 2 ? "bg-secondary/20" : ""}>
                  <td className="text-left px-3 py-2 text-foreground/80">{r.name}</td>
                  {r.values.map((v, j) => <td key={j} className="px-3 py-2 text-center tabular text-foreground/90">{v}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </motion.div>}

      {visibleSections.map((sec, i) => (
        <motion.div key={sec.title} {...stagger(3 + i)} className="mt-6">
          <h2 className="font-display border-l-[3px] border-gold pl-2.5 mb-2" style={{ fontSize: 16, fontWeight: 600 }}>{sec.title}</h2>
          {sec.pending ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin text-gold" /> 正在生成本章节…
            </div>
          ) : sec.body.trim() ? (
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/80">{sec.body}</p>
          ) : (
            // 双保险：段已到达但正文为空（后端理应已兜底回退模板，这里防任何空 body 泄漏到 UI，
            // 不让用户面对一片空白的标题）。running 时当作仍在生成，done 后如实说明本段无内容。
            running ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin text-gold" /> 正在生成本章节…
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">本章节暂无可展示内容（数据不足或撰写失败）。</p>
            )
          )}
        </motion.div>
      ))}

      {doc && <motion.div {...stagger(3 + sections.length)} className="mt-6">
        <h2 className="font-display border-l-[3px] border-up pl-2.5 mb-2 flex items-center gap-2" style={{ fontSize: 16, fontWeight: 600 }}>
          <ShieldAlert className="size-4 text-up" /> 风险提示
        </h2>
        <ul className="space-y-1.5">
          {doc.risks.map((r) => (
            <li key={r} className="flex items-start gap-2 text-sm text-foreground/80">
              <span className="text-up mt-1">·</span> {r}
            </li>
          ))}
        </ul>
      </motion.div>}

      {doc && <motion.div {...stagger(4 + sections.length)} className="mt-6">
        <h2 className="font-display border-l-[3px] border-border pl-2.5 mb-2" style={{ fontSize: 16, fontWeight: 600 }}>引用来源</h2>
        <ol className="space-y-1.5">
          {doc.sources.map((s, i) => (
            <li key={i} className="flex items-center gap-2 text-xs text-muted-foreground">
              <Link2 className="size-3 shrink-0" />
              <span className="text-foreground/70">[{i + 1}]</span>
              <span>{s.title}</span>
              <span className="font-mono text-[10px] text-muted-foreground/60">· {s.date}</span>
            </li>
          ))}
        </ol>
      </motion.div>}

      {doc && <motion.div {...stagger(5 + sections.length)} className="mt-6 pt-4 border-t border-border text-[11px] leading-relaxed text-muted-foreground/70">
        {doc.disclaimer}
      </motion.div>}
    </div>
  );
}
