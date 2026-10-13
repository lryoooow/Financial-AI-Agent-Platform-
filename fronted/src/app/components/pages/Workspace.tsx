import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Search, Sparkles, Check, Loader2, Circle, FileText, ShieldAlert, Link2, ArrowRight, Waypoints, Globe2 } from "lucide-react";
import { WATCHLIST } from "../../data/market";
import { TEMPLATES, type TemplateId, type ReportDoc } from "../../data/report";
import { fmt, signed, trendClass } from "../../lib/format";
import { api, type EvidenceStatus, type LlmConfig, type ResolveHit } from "../../api/client";
import { ModelPicker } from "../ModelPicker";

interface ActiveModel { provider: string; model: string; accent: string; }
/** 从任务列表发起的"恢复查看"请求：nonce 变化触发一次附着（同一任务可重复恢复）。 */
export interface AttachRequest { taskId: string; code: string; template: TemplateId; nonce: number; }
interface Props {
  selectedCode?: string;
  onSelectCode?: (code: string) => void;
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onReportGenerated: (r: { id: string; name: string; code: string; template: string; date: string; summary: string }) => void;
  attachRequest?: AttachRequest;
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
  callId?: string;
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

export function Workspace({ selectedCode, onSelectCode, activeModel, onGoSettings, onReportGenerated, attachRequest }: Props) {
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

  // 流式 token 的合帧缓冲：SSE 每来一帧就 setState 的话，每次重渲染的是**已累积的全部正文**，
  // 越写到后面单帧成本越高（O(n²)），表现为"先卡住、然后一次性刷出一大段"。
  // 这里把同一帧动画周期内到达的 token 按段合并，一次性提交。
  const tokenBuffer = useRef<Map<string, string>>(new Map());
  const flushHandle = useRef<number | null>(null);
  // 光标跟随目标：最近收到 token 的段（并行补写时末段可能在等模型，光标挂它会误导）
  const activeTitleRef = useRef<string | null>(null);

  const flushTokens = () => {
    flushHandle.current = null;
    const pending = tokenBuffer.current;
    if (!pending.size) return;
    tokenBuffer.current = new Map();
    // 摘要 token 走 setTyped（打字机追加）；正文 token 走 partialSections
    const summaryText = pending.get("__summary__");
    if (summaryText) {
      pending.delete("__summary__");
      setTyped((t) => t + summaryText);
    }
    if (!pending.size) return;
    setPartialSections((items) => {
      const next = items.slice();
      pending.forEach((text, title) => {
        const idx = next.findIndex((item) => item.title === title);
        if (idx === -1) next.push({ title, body: text });
        else next[idx] = { title, body: next[idx].body + text };
      });
      // 段落顺序按模板序归位：token 可能先于 section_start 到达（补写段/回放折叠帧），
      // 不排序会错位到末尾
      const order = SECTION_ORDER[displayTemplate];
      return next.sort((a, b) => {
        const ai = order.indexOf(a.title);
        const bi = order.indexOf(b.title);
        return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
      });
    });
    // 光标跟着"最近在写的段"走，而不是数组末段（多段并行补写时末段可能在等模型）
    const titles = [...pending.keys()];
    if (titles.length) activeTitleRef.current = titles[titles.length - 1];
  };

  const queueToken = (title: string, text: string) => {
    if (!text) return;
    tokenBuffer.current.set(title, (tokenBuffer.current.get(title) ?? "") + text);
    activeTitleRef.current = title;
    if (flushHandle.current == null) {
      flushHandle.current = window.requestAnimationFrame(flushTokens);
      // 后台标签页 rAF 会被暂停（回焦后缓冲一次性 flush = "整块出现"），定时器兜底
      window.setTimeout(() => {
        if (flushHandle.current != null) flushTokens();
      }, 250);
    }
  };

  /** 段收口/流结束前必须排空缓冲，否则最后一批 token 会丢。 */
  const drainTokens = () => {
    if (flushHandle.current != null) {
      window.cancelAnimationFrame(flushHandle.current);
      flushHandle.current = null;
    }
    flushTokens();
  };

  useEffect(() => () => {
    if (flushHandle.current != null) window.cancelAnimationFrame(flushHandle.current);
  }, []);

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
    // 上交所：60 主板 / 68 科创板 / 90 B股 / 11 可转债 / 50、51、52、56、58 ETF（588xxx 科创板 ETF）
    if (/^(60|68|90|11|50|51|52|56|58)/.test(pure)) return `${pure}.SH`;
    if (/^(43|83|87|88|92)/.test(pure)) return `${pure}.BJ`;
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
  // attachTaskId：恢复/附着一个已存在的任务（直连其 SSE，不重新 createResearch——
  // 避免"应用重启后误重启付费研究"）；templateOverride：附着时模板来自任务元数据
  // 而非当前 UI 状态（setState 在同一 tick 内不可见）。
  async function runBackend(resolved: CurrentStock, attachTaskId?: string, templateOverride?: TemplateId): Promise<boolean> {
    const id = ++runId.current;
    const runTemplate = templateOverride ?? template;
    setDisplayTemplate(runTemplate);
    setStock(resolved);
    if (runTemplate !== "industry") onSelectCode?.(resolved.code);
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
    if (attachTaskId) {
      // 恢复附着：任务已存在，直连其流（后端回放 history + 订阅直播）
      taskId = attachTaskId;
    } else {
      try {
        const r = await api.createResearch(resolved.code, runTemplate, undefined, llm ?? undefined);
        taskId = r.task_id;
      } catch {
        setError("无法提交研究任务：后端服务不可用或真实数据接口异常。");
        setPhase("idle");
        return false;
      }
    }
    if (runId.current !== id) return true;

    emit(`提交研究任务 ${taskId.slice(0, 8)}…`);
    await new Promise<void>((resolve) => {
      const es = new EventSource(api.researchStreamUrl(taskId, resolved.code, runTemplate));
      // 重连语义：后端为断线设计了 history 回放（段级折叠后一份完整报告都能装下），
      // EventSource 断开后会自动重连。这里不在首个 onerror 上放弃——那会把仍在后台
      // 正常运行的任务打成"失败"，过一会儿报告又"自己冒出来"，两处状态互相矛盾。
      let consecutiveErrors = 0;
      let terminal = false;
      const finish = () => { try { es.close(); } catch {} resolve(); };
      es.onmessage = (e: MessageEvent) => {
        if (runId.current !== id || terminal) { finish(); return; }
        consecutiveErrors = 0; // 有帧到达即视为连接健康
        let ev: any;
        try { ev = JSON.parse(e.data); } catch { return; }
        if (ev.type === "tool_call") {
          // 工具调用配对：优先按 call_id 精确匹配，无 id 时配最旧的 start 同名芯片（FIFO）
          setToolCalls((calls) => {
            const callId = (ev as any).call_id || "";
            if (ev.status === "start") {
              // 去重：同 call_id 已有芯片则不追加
              if (callId && calls.some((c) => c.callId === callId)) return calls;
              const placeholder = calls.length === 1 && calls[0].name === "_task" ? calls[0].events : [];
              const base = calls.filter((c) => c.name !== "_task");
              return [...base, { name: ev.name, label: ev.label || ev.name, status: "start", events: placeholder, callId }];
            }
            // ok/error：按 call_id 精确配对
            let idx = -1;
            if (callId) {
              idx = calls.findIndex((c) => c.callId === callId);
            }
            // 无 id 时配最旧的 start 同名芯片（FIFO，对顺序重复调用正确）
            if (idx === -1) {
              idx = calls.findIndex((c) => c.name === ev.name && c.status === "start");
            }
            // 兜底：最后一条同名
            if (idx === -1) {
              idx = calls.findLastIndex?.((c) => c.name === ev.name) ?? -1;
            }
            if (idx >= 0) {
              const next = [...calls];
              next[idx] = { ...next[idx], status: ev.status };
              return next;
            }
            return calls; // 配不上不追加（terminal replay 已兜底）
          });
        } else if (ev.type === "todo") {
          setTodos(ev.items ?? []);
        } else if (ev.type === "tool") {
          emit(ev.text);
        } else if (ev.type === "summary") {
          drainTokens(); // 排空残留的摘要 token，防替换后追加闪变
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
        } else if (ev.type === "summary_token") {
          // 摘要逐 token 流（打字机效果）：走与 section_token 相同的 rAF 合帧通道
          queueToken("__summary__", ev.text ?? "");
        } else if (ev.type === "token") {
          // 兼容旧后端；新后端使用 summary 快照，重连时不会重复 append。
          setTyped((t) => t + (ev.text ?? ""));
        } else if (ev.type === "section_start") {
          // 某部分开始撰写，建一个空 body 的段并按模板序插入。
          // 已存在的段必须清空 body：整篇失败降级到分段重写时会重发 section_start，
          // 旧实现"已存在则原样返回"会把被中止的半截正文与重写正文拼成脏流。
          const title = ev.title ?? "分析章节";
          drainTokens();   // 切段前把上一段的残留 token 落盘，避免串到新段
          setPartialSections((items) =>
            (items.some((item) => item.title === title)
              ? items.map((item) => (item.title === title ? { title, body: "" } : item))
              : [...items, { title, body: "" }]
            ).sort((a, b) => {
              const order = SECTION_ORDER[runTemplate];
              const ai = order.indexOf(a.title);
              const bi = order.indexOf(b.title);
              return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
            })
          );
        } else if (ev.type === "section_token") {
          // 逐 token 追加（按帧合并后提交，见 queueToken）
          queueToken(ev.title ?? "分析章节", ev.text ?? "");
        } else if (ev.type === "section") {
          // 权威收口：用最终 body 覆盖流式累积（漏段回退/降级后以此为准）
          const section = { title: ev.title ?? "分析章节", body: ev.body ?? "" };
          drainTokens();
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
          if (terminal) { finish(); return; }
          terminal = true; // 重连回放会再次送达 done：报告入库回调只许触发一次
          // 终态兜底：任务已结束，仍处于 start 的芯片不可能再等到 ok 帧
          // （丢帧/重复 start 配对失败/异常路径），统一收口为完成态，杜绝永久转圈。
          setToolCalls((calls) => calls.map((c) => (c.status === "start" ? { ...c, status: "ok" } : c)));
          const rep = ev.report;
          const reportId = ev.report_id || `${resolved.code}-${Date.now()}`;
          drainTokens();   // 排空最后一批 token，否则末尾会缺一小段
          setDoc(rep);
          setStock((current) => ({
            ...current,
            code: rep?.ticker || current.code,
            name: rep?.name || current.name,
          }));
          if (rep?.ticker) onSelectCode?.(rep.ticker);
          setPhase("done");
          reportRef.current?.scrollTo({ top: 0 });
          onReportGenerated({ id: reportId, name: rep?.name || resolved.name, code: rep?.ticker || resolved.code, template: TEMPLATES.find((t) => t.id === runTemplate)!.label, date: rep?.as_of ?? new Date().toLocaleString("zh-CN", { hour12: false }), summary: rep?.summary ?? "" });
          finish();
        } else if (ev.type === "error") {
          if (terminal) { finish(); return; }
          terminal = true;
          // 终态兜底：失败的 run 里没跑完的工具芯片标 error，不再永久转圈
          setToolCalls((calls) => calls.map((c) => (c.status === "start" ? { ...c, status: "error" } : c)));
          const message = ev.message ?? "研究管线失败";
          drainTokens();   // 出错也要保留已经流出来的内容
          emit(`⚠ ${message}`);
          setError(message);
          setPhase("idle");
          finish();
        }
      };
      es.onerror = () => {
        if (runId.current !== id || terminal) { finish(); return; }
        consecutiveErrors += 1;
        if (consecutiveErrors >= 6) {
          setError("研究流连接中断（多次重连失败）。任务可能仍在后台运行，完成后会出现在报告库。");
          setPhase("idle");
          finish();
          return;
        }
        // 重连成功后后端整段回放 history：本地清空待回放重建，
        // 避免工具轨迹/段落/摘要与已渲染内容重复叠加
        setToolCalls([]);
        setTodos([]);
        setPartialSections([]);
        setTyped("");
        emit(`研究流中断，正在重连（${consecutiveErrors}/6）…`);
      };
    });
    return true;
  }

  // 挂载时自动恢复运行中的研究任务（刷新/切页回来不丢进度）。
  // 直连任务 SSE 附着而非重新提交：后端回放 history + 订阅直播帧；
  // 应用重启后的孤儿任务会被后端如实标记中断（终态 error 帧），不会误启新的付费研究。
  const autoResumed = useRef(false);
  useEffect(() => {
    if (autoResumed.current) return;
    autoResumed.current = true;
    api.researchTasks()
      .then(({ tasks }) => {
        // 取最近一条有意义的任务（后端已按 updated_at desc 排序）
        // running/pending → 附着直播流；succeeded → 附着终态回放（后端发完整 done 帧）
        const active = tasks.find(
          (t) => t.status === "running" || t.status === "pending"
            || (t.status === "succeeded" && t.report_id)
        );
        if (!active) return;
        setQuery(active.code);
        setTemplate(active.template as TemplateId);
        void runBackend(
          { code: active.code, name: active.code },
          active.id,
          active.template as TemplateId,
        );
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 任务列表发起的"恢复查看"（ResearchTasks 页）
  useEffect(() => {
    if (!attachRequest) return;
    setQuery(attachRequest.code);
    setTemplate(attachRequest.template);
    void runBackend({ code: attachRequest.code, name: attachRequest.code }, attachRequest.taskId, attachRequest.template);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attachRequest?.nonce]);

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

  // 终态强制收口：phase 变为 done/error 时，所有 start 芯片必定变绿/变红。
  // 曾经只在 done 事件的内联 setToolCalls 里做——如果 React 批处理/时序
  // 导致那次 setState 被跳过（或在 finish() 之后执行），芯片就永远转圈。
  // useEffect 在渲染后必定执行，是结构性兜底。
  useEffect(() => {
    if (phase === "done") {
      setToolCalls((calls) => calls.map((c) => (c.status === "start" ? { ...c, status: "ok" as const } : c)));
    } else if (phase === "idle") {
      // error 路径 phase 回到 idle
      setToolCalls((calls) => calls.map((c) => (c.status === "start" ? { ...c, status: "error" as const } : c)));
    }
  }, [phase]);

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
              <button key={s.code} onClick={() => { setQuery(s.code); onSelectCode?.(s.code); }} className="text-[11px] px-2 py-1 rounded-md bg-secondary/60 hover:bg-secondary text-muted-foreground hover:text-foreground transition-colors">
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
            activeTitle={phase === "running" ? activeTitleRef.current : undefined}
          />
        )}
      </div>
    </div>
  );
}

function ResearchReportView({ doc, stock, summary, sections, template, running, activeTitle }: {
  doc: ReportDoc | null;
  stock: CurrentStock;
  summary: string;
  sections: { title: string; body: string }[];
  template: TemplateId;
  running: boolean;
  activeTitle?: string;
}) {
  // 生成中不做入场延迟：正文是逐字流进来的，再叠一个 0.08*i 的错峰动画，
  // 段落会慢半拍才浮现，看起来就像"一段一段蹦出来"。
  const stagger = (i: number) => ({
    initial: { opacity: 0, y: 14 },
    animate: { opacity: 1, y: 0 },
    transition: { delay: running ? 0 : 0.08 * i, duration: running ? 0.2 : 0.5, ease: [0.16, 1, 0.3, 1] as any },
  });
  // 只渲染真正开始撰写的部分。
  //
  // 此前这里按 SECTION_ORDER 把全部标题预先摆出来，未到达的显示"正在生成本章节…"转圈——
  // 于是一开门就是一副空骨架 + 四个转圈，然后被逐格填满。后端早已改成一次写完整篇，
  // 但这层把它重新渲染成了"分章节等待"，观感反而更乱。
  // 现在标题随正文一起出现：写到哪、显示到哪，从头到尾一条连续的文章。
  const visibleSections = sections;
  const observationLabel = template === "risk" ? "监测重点" : template === "earnings" ? "后续观察" : template === "industry" ? "行业催化" : "催化剂";
  return (
    <div className="p-6 max-w-3xl mx-auto">
      <motion.div {...stagger(0)} className="border-b border-gold/30 pb-4">
        <div className="flex items-center gap-2 text-[11px] text-gold mb-1.5">
          <span className="px-2 py-0.5 rounded bg-accent">{TEMPLATES.find((t) => t.id === template)!.label}</span>
          <span className={`flex items-center gap-1 ${running ? "text-gold" : "text-down"}`}>
            {running ? <Loader2 className="size-3 animate-spin" /> : <Check className="size-3" />}
            {running ? "连续撰写中" : "数字零幻觉 · 可溯源"}
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

      {visibleSections.map((sec, i) => {
        const isActive = running && sec.title === (activeTitle ?? visibleSections[visibleSections.length - 1]?.title);
        return (
          <motion.div key={sec.title} {...stagger(3 + i)} className="mt-6">
            <h2 className="font-display border-l-[3px] border-gold pl-2.5 mb-2" style={{ fontSize: 16, fontWeight: 600 }}>{sec.title}</h2>
            {sec.body.trim() ? (
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/80">
                {sec.body}
                {/* 光标只跟在正在写的那一段末尾，标记"笔停在这里"，而不是每段都挂一个转圈 */}
                {isActive && (
                  <span className="ml-0.5 inline-block h-4 w-1.5 -mb-0.5 animate-pulse rounded-sm bg-gold align-middle" />
                )}
              </p>
            ) : running ? (
              // 标题刚到、正文还没流出来的那一瞬间：只留光标，不要转圈+文案，
              // 否则每段开头都会闪一下"正在生成本章节…"，又变回分段等待的观感。
              <p className="text-sm leading-relaxed">
                <span className="inline-block h-4 w-1.5 -mb-0.5 animate-pulse rounded-sm bg-gold align-middle" />
              </p>
            ) : (
              // 双保险：段已到达但正文为空（后端理应已兜底回退模板，这里防任何空 body 泄漏到 UI）
              <p className="text-xs text-muted-foreground">本章节暂无可展示内容（数据不足或撰写失败）。</p>
            )}
          </motion.div>
        );
      })}

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
