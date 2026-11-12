import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import {
  AlertCircle,
  ArrowRight,
  BookOpen,
  Building2,
  CheckCircle2,
  Clock3,
  FileBarChart2,
  FlaskConical,
  Loader2,
  MessageSquare,
  Play,
  Plus,
  Search,
  ShieldAlert,
  Sparkles,
  Square,
  Trash2,
  TrendingUp,
} from "lucide-react";
import {
  api,
  type LlmConfig,
  type ResearchTaskSummary,
  type StockHistoryItem,
} from "../../api/client";
import { TEMPLATES, type TemplateId } from "../../data/report";
import { ModelPicker, loadActiveLlm } from "../ModelPicker";
import { ResearchCard, SECTION_ORDER } from "../research/ResearchCard";
import { isIndexCodeInput, resolveResearchTarget } from "../../lib/ticker";

export interface PendingResearch {
  code: string;
  name: string;
  nonce: number;
  template?: TemplateId;
  autoStart?: boolean;
}

interface ActiveModel {
  provider: string;
  model: string;
  accent: string;
}

interface CandidateStock {
  code: string;
  name: string;
  exchange?: string;
}

const TEMPLATE_META: Record<
  TemplateId,
  {
    icon: typeof TrendingUp;
    tagline: string;
    detail: string;
  }
> = {
  equity_deep: {
    icon: TrendingUp,
    tagline: "全景四维研报",
    detail: "自动拉取实时行情、60 日 K 线技术指标、多年财务报表与近期快讯，一次流式生成完整深度研报。",
  },
  industry: {
    icon: Building2,
    tagline: "赛道景气与格局",
    detail: "围绕申万行业分类梳理景气周期、核心驱动、竞争格局、估值水位与潜在风险。",
  },
  earnings: {
    icon: FileBarChart2,
    tagline: "核心财报拆解",
    detail: "聚焦最新财报期营收、归母净利、ROE、毛利率与现金流变化，快速定位经营拐点。",
  },
  risk: {
    icon: ShieldAlert,
    tagline: "下行压力与排雷",
    detail: "从估值溢价、技术超买、波动率、流动性及基本面脆弱点排查潜在回撤情景与监测信号。",
  },
};

const HOT_STOCKS: { code: string; name: string }[] = [
  { code: "600519.SH", name: "贵州茅台" },
  { code: "300750.SZ", name: "宁德时代" },
  { code: "002594.SZ", name: "比亚迪" },
  { code: "600036.SH", name: "招商银行" },
  { code: "601318.SH", name: "中国平安" },
  { code: "000858.SZ", name: "五粮液" },
];

const HOT_INDUSTRIES = [
  "半导体",
  "光伏设备",
  "人工智能",
  "白酒",
  "新能源汽车",
  "创新药",
  "消费电子",
  "银行",
];

const FOCUS_PRESETS: Record<TemplateId, string[]> = {
  equity_deep: [
    "重点拆解毛利率与ROE变化趋势及可持续性",
    "评估当前PE/PB估值安全边际与分红回报",
    "梳理近期核心催化剂与主要下行风险",
    "对比同行业龙头公司的核心竞争壁垒",
  ],
  industry: [
    "判断当前行业处于景气周期的哪个阶段",
    "梳理产业链上下游利润分配与国产替代进展",
    "对比板块内核心龙头估值与盈利增速匹配度",
  ],
  earnings: [
    "核查营收与净利润增速背离或共振的核心原因",
    "分析经营性现金流与净利润的匹配质量",
    "判断单季毛利率与费用率是否出现拐点",
  ],
  risk: [
    "排查高估值回撤、业绩不及预期与流动性风险",
    "给出关键支撑位与基本面恶化的预警观测指标",
    "评估极端市场环境下的抗跌韧性",
  ],
};

function formatTaskTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" })
    : d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" });
}

function templateLabel(id: string): string {
  return TEMPLATES.find((t) => t.id === id)?.label ?? "深度研究";
}

export function ResearchHub({
  activeModel,
  visible = true,
  defaultTemplate = "equity_deep",
  pendingResearch,
  newTaskNonce = 0,
  onGoSettings,
  onOpenReports,
  onAskInChat,
  onReportGenerated,
  onRunningCountChange,
}: {
  activeModel: ActiveModel | null;
  visible?: boolean;
  defaultTemplate?: TemplateId;
  pendingResearch?: PendingResearch | null;
  newTaskNonce?: number;
  onGoSettings: () => void;
  onOpenReports: () => void;
  onAskInChat: (prompt: string) => void;
  onReportGenerated?: (r: { id: string; name: string; code: string; template: string; date: string; summary: string }) => void;
  onRunningCountChange?: (count: number) => void;
}) {
  const [template, setTemplate] = useState<TemplateId>(defaultTemplate);
  const [targetInput, setTargetInput] = useState("");
  const [pickedTarget, setPickedTarget] = useState<{ code: string; name: string } | null>(null);
  const [focusQuery, setFocusQuery] = useState("");
  const [llm, setLlm] = useState<LlmConfig | null>(() => loadActiveLlm());

  const [candidates, setCandidates] = useState<CandidateStock[]>([]);
  const [searchingStock, setSearchingStock] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [recentStocks, setRecentStocks] = useState<StockHistoryItem[]>([]);

  const [tasks, setTasks] = useState<ResearchTaskSummary[]>([]);
  const [taskFilter, setTaskFilter] = useState<"all" | "running" | "done">("all");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [pollWarning, setPollWarning] = useState("");

  const submittingRef = useRef(false);
  const searchSeqRef = useRef(0);
  const refreshSeqRef = useRef(0);
  const locallySettledRef = useRef<
    Map<string, { status: "done" | "error"; report_id?: string | null; name?: string; at: number }>
  >(new Map());
  const prevRunningIdsRef = useRef<Set<string>>(new Set());
  const onReportGeneratedRef = useRef(onReportGenerated);
  onReportGeneratedRef.current = onReportGenerated;
  const searchBoxRef = useRef<HTMLDivElement | null>(null);
  const targetInputRef = useRef<HTMLInputElement | null>(null);
  const handledPendingRef = useRef(0);
  const handledNewNonceRef = useRef(newTaskNonce);

  useEffect(() => {
    setTemplate(defaultTemplate);
  }, [defaultTemplate]);

  const settleTaskLocally = useCallback(
    (
      taskId: string,
      patch: { status: "done" | "error"; report_id?: string | null; name?: string; error?: string | null },
    ) => {
      locallySettledRef.current.set(taskId, {
        status: patch.status,
        report_id: patch.report_id,
        name: patch.name,
        at: Date.now(),
      });
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId
            ? {
                ...t,
                status: patch.status,
                stage: patch.status,
                report_id: patch.report_id !== undefined ? patch.report_id : t.report_id,
                name: patch.name || t.name,
                error: patch.error !== undefined ? patch.error : t.error,
                updated_at: new Date().toISOString(),
              }
            : t,
        ),
      );
    },
    [],
  );

  const refreshTasks = useCallback(async () => {
    const seq = ++refreshSeqRef.current;
    try {
      const res = await api.researchTasks();
      if (seq !== refreshSeqRef.current) return [];
      const nowMs = Date.now();
      for (const [k, v] of locallySettledRef.current.entries()) {
        if (nowMs - v.at > 20_000) locallySettledRef.current.delete(k);
      }
      const rawList = Array.isArray(res.tasks) ? res.tasks : [];
      const list = rawList.map((t) => {
        const local = locallySettledRef.current.get(t.id);
        if (local && (t.status === "running" || t.status === "pending")) {
          return {
            ...t,
            status: local.status,
            stage: local.status,
            report_id: local.report_id ?? t.report_id,
            name: local.name || t.name,
          };
        }
        if (local && t.status === local.status) {
          locallySettledRef.current.delete(t.id);
        }
        return t;
      });

      // 检测后台完成（非当前打开卡片）的任务，通知报告库刷新
      const prevRunning = prevRunningIdsRef.current;
      for (const t of list) {
        if (prevRunning.has(t.id) && t.status === "done" && t.report_id) {
          onReportGeneratedRef.current?.({
            id: t.report_id,
            name: t.name || t.code,
            code: t.code,
            template: templateLabel(t.template),
            date: t.updated_at || t.created_at,
            summary: "",
          });
        }
      }
      prevRunningIdsRef.current = new Set(
        list.filter((t) => t.status === "running" || t.status === "pending").map((t) => t.id),
      );

      setPollWarning("");
      setTasks(list);
      const runningCount = list.filter((t) => t.status === "running" || t.status === "pending").length;
      onRunningCountChange?.(runningCount);
      return list;
    } catch {
      if (seq === refreshSeqRef.current) {
        setPollWarning("任务队列状态同步延迟，已保留本地最新进度");
      }
      return [];
    }
  }, [onRunningCountChange]);

  const loadRecentStocks = useCallback(() => {
    api.marketHistory(6)
      .then((r) => setRecentStocks(r.recent || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    void refreshTasks();
    loadRecentStocks();
  }, [refreshTasks, loadRecentStocks]);

  useEffect(() => {
    if (!visible) return;
    void refreshTasks();
    loadRecentStocks();
  }, [visible, refreshTasks, loadRecentStocks]);

  // 有后台运行中任务时定时同步队列状态，确保左侧导航角标与任务栏实时准确
  const hasRunning = useMemo(
    () => tasks.some((t) => t.status === "running" || t.status === "pending"),
    [tasks],
  );
  useEffect(() => {
    const runningCount = tasks.filter((t) => t.status === "running" || t.status === "pending").length;
    onRunningCountChange?.(runningCount);
  }, [tasks, onRunningCountChange]);

  useEffect(() => {
    if (!hasRunning) return;
    const id = window.setInterval(() => {
      if (!document.hidden) void refreshTasks();
    }, 4000);
    const onVis = () => {
      if (!document.hidden) void refreshTasks();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [hasRunning, refreshTasks]);

  // 跨页面（如投研问答页内嵌研究）发起研究任务时即时同步左侧任务栏与角标
  useEffect(() => {
    const onCreated = () => {
      void refreshTasks();
    };
    window.addEventListener("finresearch:research-created", onCreated);
    return () => window.removeEventListener("finresearch:research-created", onCreated);
  }, [refreshTasks]);

  // 报告库删除报告时联动刷新任务队列
  useEffect(() => {
    const onDeleted = (e: Event) => {
      const detail = (e as CustomEvent<{ id?: string; taskId?: string }>).detail;
      void refreshTasks();
      if (detail?.taskId && detail.taskId === selectedTaskId) {
        setSelectedTaskId(null);
      }
    };
    window.addEventListener("finresearch:report-deleted", onDeleted);
    return () => window.removeEventListener("finresearch:report-deleted", onDeleted);
  }, [refreshTasks, selectedTaskId]);

  // 外部（如报告库「新建研究」）请求打开新建面板
  useEffect(() => {
    if (newTaskNonce !== handledNewNonceRef.current) {
      handledNewNonceRef.current = newTaskNonce;
      setSelectedTaskId(null);
      setError("");
      window.setTimeout(() => targetInputRef.current?.focus(), 60);
    }
  }, [newTaskNonce]);

  // 点击搜索框外部关闭候选下拉
  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      if (searchBoxRef.current && !searchBoxRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, []);

  // 个股模板下的实时搜股联想（支持代码 / 简称 / 拼音首字母）
  useEffect(() => {
    if (template === "industry") {
      setCandidates([]);
      setDropdownOpen(false);
      return;
    }
    const q = targetInput.trim();
    if (!q || (pickedTarget && `${pickedTarget.name} (${pickedTarget.code})` === q)) {
      setCandidates([]);
      setSearchingStock(false);
      return;
    }
    const seq = ++searchSeqRef.current;
    const timer = window.setTimeout(async () => {
      setSearchingStock(true);
      try {
        const res = await api.resolve(q);
        if (seq !== searchSeqRef.current) return;
        const list = (Array.isArray(res) ? res : []).slice(0, 8).map((item) => ({
          code: item.code,
          name: item.name,
          exchange: item.exchange,
        }));
        setCandidates(list);
        setDropdownOpen(list.length > 0);
      } catch {
        if (seq === searchSeqRef.current) setCandidates([]);
      } finally {
        if (seq === searchSeqRef.current) setSearchingStock(false);
      }
    }, 180);
    return () => window.clearTimeout(timer);
  }, [targetInput, template, pickedTarget]);

  const launchResearch = useCallback(
    async (opts: {
      rawTarget: string;
      tpl: TemplateId;
      queryText?: string;
      known?: { code: string; name: string } | null;
    }) => {
      if (submittingRef.current) return;
      const effectiveLlm = llm ?? loadActiveLlm();
      if (!effectiveLlm || !effectiveLlm.api_key?.trim() || !effectiveLlm.base_url?.trim()) {
        setSelectedTaskId(null);
        setError("尚未配置可用大模型：请先前往「设置 → 模型配置」填入 API Key 并选择模型。");
        return;
      }

      const raw = opts.rawTarget.trim();
      if (!raw && !opts.known) {
        setError(opts.tpl === "industry" ? "请输入要研究的行业或赛道名称，如「半导体」「光伏设备」。" : "请输入要研究的 A 股代码、名称或拼音首字母，如「600519」或「贵州茅台」。");
        return;
      }

      submittingRef.current = true;
      setSubmitting(true);
      setError("");
      setDropdownOpen(false);
      try {
        let target = opts.known ?? null;
        if (!target) {
          if (opts.tpl !== "industry" && isIndexCodeInput(raw)) {
            setError("当前个股模板仅支持 A 股个股 / ETF 研究（暂不支持 000001.SH / 399001 等宽基指数），请输入个股或切换为「行业研究」。");
            return;
          }
          target = await resolveResearchTarget(raw, opts.tpl);
        }
        if (!target) {
          setError(opts.tpl === "industry" ? "请输入有效的行业名称，如「半导体」「白酒」。" : `未匹配到真实 A 股标的：${raw}`);
          return;
        }

        const cleanQuery = (opts.queryText || "").trim() || undefined;
        const created = await api.createResearch(
          target.code,
          opts.tpl,
          cleanQuery,
          effectiveLlm,
          { name: target.name },
        );

        const optimisticTask: ResearchTaskSummary = {
          id: created.task_id,
          code: target.code,
          name: target.name,
          template: opts.tpl,
          query: cleanQuery ?? null,
          status: "running",
          stage: "pending",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        setTasks((prev) => [optimisticTask, ...prev.filter((x) => x.id !== created.task_id)]);
        setSelectedTaskId(created.task_id);
        setTargetInput("");
        setPickedTarget(null);
        setFocusQuery("");
        void refreshTasks();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        submittingRef.current = false;
        setSubmitting(false);
      }
    },
    [llm, refreshTasks],
  );

  // 接收从「实时行情」页面一键带过来的标的
  useEffect(() => {
    if (!pendingResearch || handledPendingRef.current === pendingResearch.nonce) return;
    handledPendingRef.current = pendingResearch.nonce;
    const baseTpl = pendingResearch.template ?? template;
    const stockTpl: TemplateId = baseTpl === "industry" ? "equity_deep" : baseTpl;
    setTemplate(stockTpl);
    setPickedTarget({ code: pendingResearch.code, name: pendingResearch.name });
    setTargetInput(`${pendingResearch.name} (${pendingResearch.code})`);
    setSelectedTaskId(null);

    if (pendingResearch.autoStart !== false) {
      const effectiveLlm = llm ?? loadActiveLlm();
      if (effectiveLlm && effectiveLlm.api_key?.trim()) {
        void launchResearch({
          rawTarget: pendingResearch.code,
          tpl: stockTpl,
          known: { code: pendingResearch.code, name: pendingResearch.name },
        });
      } else {
        setError(`已为你选定「${pendingResearch.name} (${pendingResearch.code})」，请先配置模型 API Key 后即可一键启动研究。`);
      }
    }
  }, [pendingResearch, template, llm, launchResearch]);

  const activeTask = useMemo(
    () => (selectedTaskId ? tasks.find((t) => t.id === selectedTaskId) ?? null : null),
    [tasks, selectedTaskId],
  );

  const filteredTasks = useMemo(() => {
    if (taskFilter === "running") return tasks.filter((t) => t.status === "running" || t.status === "pending");
    if (taskFilter === "done") return tasks.filter((t) => t.status === "done");
    return tasks;
  }, [tasks, taskFilter]);

  const runningCount = useMemo(
    () => tasks.filter((t) => t.status === "running" || t.status === "pending").length,
    [tasks],
  );
  const doneCount = useMemo(() => tasks.filter((t) => t.status === "done").length, [tasks]);

  const quickStocks = useMemo(() => {
    if (recentStocks.length > 0) {
      const merged = recentStocks.map((s) => ({ code: s.code, name: s.name || s.code }));
      for (const h of HOT_STOCKS) {
        if (merged.length >= 6) break;
        if (!merged.some((m) => m.code === h.code)) merged.push(h);
      }
      return merged;
    }
    return HOT_STOCKS;
  }, [recentStocks]);

  async function handleDeleteTask(t: ResearchTaskSummary, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api.deleteResearchTask(t.id);
      setTasks((prev) => prev.filter((x) => x.id !== t.id));
      if (selectedTaskId === t.id) setSelectedTaskId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleCancelTask(t: ResearchTaskSummary, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api.cancelResearch(t.id);
      await refreshTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const selectStockCandidate = (c: CandidateStock) => {
    setPickedTarget({ code: c.code, name: c.name });
    setTargetInput(`${c.name} (${c.code})`);
    setDropdownOpen(false);
    setError("");
  };

  return (
    <div className="h-full min-h-0 grid grid-cols-1 md:grid-cols-[272px_minmax(0,1fr)] overflow-hidden">
      {/* 左侧：深度研究任务队列与历史轨 */}
      <aside className="border-r border-border bg-card/50 flex flex-col min-h-0">
        <div className="p-3 border-b border-border space-y-2.5">
          <button
            type="button"
            onClick={() => {
              setSelectedTaskId(null);
              setError("");
              window.setTimeout(() => targetInputRef.current?.focus(), 40);
            }}
            className={`w-full flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium transition-all ${
              selectedTaskId === null
                ? "bg-gold text-[#17140a] shadow-sm"
                : "bg-gold/90 text-[#17140a] hover:bg-gold hover:brightness-105"
            }`}
          >
            <Plus className="size-3.5" strokeWidth={2.5} /> 新建深度研究任务
          </button>

          <div className="grid grid-cols-3 rounded-lg bg-secondary/60 p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setTaskFilter("all")}
              className={`rounded-md py-1 transition-colors ${
                taskFilter === "all" ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              全部 ({tasks.length})
            </button>
            <button
              type="button"
              onClick={() => setTaskFilter("running")}
              className={`rounded-md py-1 transition-colors ${
                taskFilter === "running" ? "bg-card text-gold shadow-sm" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              进行中 ({runningCount})
            </button>
            <button
              type="button"
              onClick={() => setTaskFilter("done")}
              className={`rounded-md py-1 transition-colors ${
                taskFilter === "done" ? "bg-card text-down shadow-sm" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              已完成 ({doneCount})
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {pollWarning && (
            <div className="mx-1 mb-1 rounded-md border border-gold/25 bg-gold/10 px-2 py-1 text-[10.5px] text-gold">
              {pollWarning}
            </div>
          )}
          {filteredTasks.length === 0 && (
            <div className="px-3 py-10 text-center space-y-2">
              <FlaskConical className="size-6 text-muted-foreground/40 mx-auto" />
              <p className="text-xs text-muted-foreground">
                {taskFilter === "running"
                  ? "当前没有正在运行的研究任务"
                  : taskFilter === "done"
                    ? "暂无已完成的研究任务"
                    : "在右侧选择模板并输入股票或行业，即可启动第一份深度研报"}
              </p>
            </div>
          )}

          {filteredTasks.map((t) => {
            const isSelected = selectedTaskId === t.id;
            const isRunning = t.status === "running" || t.status === "pending";
            const displayName = t.name && t.name !== t.code ? t.name : t.code;
            const showCode = t.name && t.name !== t.code ? t.code : "";
            return (
              <div
                key={t.id}
                onClick={() => {
                  setSelectedTaskId(t.id);
                  setError("");
                }}
                className={`group rounded-lg px-2.5 py-2.5 cursor-pointer border transition-colors ${
                  isSelected
                    ? "bg-accent border-gold/40"
                    : "border-transparent hover:bg-secondary/60"
                }`}
              >
                <div className="flex items-center gap-2">
                  {isRunning ? (
                    <Loader2 className="size-3.5 text-gold animate-spin shrink-0" />
                  ) : t.status === "done" ? (
                    <CheckCircle2 className="size-3.5 text-down shrink-0" />
                  ) : (
                    <AlertCircle className="size-3.5 text-up shrink-0" />
                  )}
                  <span className="text-xs font-medium truncate flex-1">{displayName}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground shrink-0">
                    {templateLabel(t.template)}
                  </span>
                </div>

                <div className="mt-1 pl-5.5 flex items-center justify-between gap-2 text-[10.5px] text-muted-foreground">
                  <span className="truncate font-mono">
                    {showCode || (isRunning ? "实时撰写中…" : t.status === "done" ? "已归档入库" : "未完成")}
                  </span>
                  <div className="flex items-center gap-1 shrink-0">
                    <span className="text-[10px] text-muted-foreground/70">{formatTaskTime(t.updated_at || t.created_at)}</span>
                    {isRunning && (
                      <button
                        type="button"
                        onClick={(e) => void handleCancelTask(t, e)}
                        title="取消该任务"
                        className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:bg-destructive/15 text-muted-foreground hover:text-destructive transition-all"
                      >
                        <Square className="size-3 fill-current" />
                      </button>
                    )}
                    {!isRunning && !t.report_id && (
                      <button
                        type="button"
                        onClick={(e) => void handleDeleteTask(t, e)}
                        title="清理该条未完成记录"
                        className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:bg-destructive/15 text-muted-foreground hover:text-destructive transition-all"
                      >
                        <Trash2 className="size-3" />
                      </button>
                    )}
                  </div>
                </div>

                {t.query && (
                  <p className="mt-1 pl-5.5 text-[10.5px] text-muted-foreground/80 truncate" title={t.query}>
                    关注：{t.query}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </aside>

      {/* 右侧：深度研究主工作区 */}
      <section className="flex flex-col min-h-0 overflow-hidden bg-background">
        {/* 顶栏 */}
        <header className="px-5 py-3 border-b border-border flex items-center justify-between gap-3 shrink-0 bg-card/30">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="grid place-items-center size-8 rounded-lg bg-gold/15 border border-gold/30 text-gold shrink-0">
              <FlaskConical className="size-4" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-semibold truncate">深度研究任务台</h1>
                <span className="hidden sm:inline-flex items-center gap-1 rounded-full bg-gold/12 border border-gold/30 px-2 py-0.5 text-[10px] text-gold">
                  <Sparkles className="size-2.5" /> 多源实盘数据 · 流式成稿
                </span>
              </div>
              <p className="text-[11px] text-muted-foreground truncate">
                自动调取实时行情、财务报表、技术指标与快讯公告，逐章流式输出结构化研报并沉淀至本地知识库
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <ModelPicker active={activeModel} onGoSettings={onGoSettings} onChange={setLlm} />
          </div>
        </header>

        {/* 主体内容区 */}
        <div className="flex-1 overflow-y-auto p-5">
          {activeTask ? (
            /* ── 视图 B：单任务实时流式/成品阅读视图 ── */
            <div className="mx-auto max-w-4xl space-y-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedTaskId(null);
                      setError("");
                    }}
                    className="flex items-center gap-1.5 rounded-lg border border-border bg-card hover:bg-secondary/60 px-3 py-1.5 text-xs transition-colors"
                  >
                    <Plus className="size-3.5 text-gold" /> 再建一份深度研究
                  </button>
                  {activeTask.query && (
                    <span className="inline-flex items-center gap-1.5 rounded-lg border border-gold/25 bg-accent/40 px-2.5 py-1 text-xs text-foreground/85">
                      <Sparkles className="size-3 text-gold shrink-0" />
                      <span className="truncate max-w-md">研究关注：{activeTask.query}</span>
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  {activeTask.status === "done" && (
                    <button
                      type="button"
                      onClick={() => {
                        const who =
                          activeTask.name && activeTask.name !== activeTask.code
                            ? `${activeTask.name}（${activeTask.code}）`
                            : activeTask.code;
                        onAskInChat(
                          `结合刚刚生成的《${who} · ${templateLabel(activeTask.template)}》研究报告，请进一步帮我深入分析：`,
                        );
                      }}
                      className="flex items-center gap-1.5 rounded-lg bg-gold/15 border border-gold/35 hover:bg-gold/25 px-3 py-1.5 text-xs text-gold transition-colors"
                    >
                      <MessageSquare className="size-3.5" /> 去投研问答追问此报告
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={onOpenReports}
                    className="flex items-center gap-1.5 rounded-lg border border-border bg-card hover:bg-secondary/60 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <BookOpen className="size-3.5" /> 打开报告库
                  </button>
                </div>
              </div>

              <ResearchCard
                key={activeTask.id}
                defaultTrailOpen={activeTask.status === "running" || activeTask.status === "pending"}
                initialStatus={activeTask.status}
                initialReportId={activeTask.report_id}
                initialError={activeTask.error}
                research={{
                  task_id: activeTask.id,
                  code: activeTask.code,
                  template: activeTask.template,
                  name: activeTask.name || activeTask.code,
                }}
                onStatusChange={(p) => {
                  if (p === "done" || p === "error") {
                    settleTaskLocally(activeTask.id, { status: p });
                    void refreshTasks();
                  }
                }}
                onDone={(r) => {
                  settleTaskLocally(activeTask.id, {
                    status: "done",
                    report_id: r.id,
                    name: r.name,
                  });
                  void refreshTasks();
                  onReportGenerated?.(r);
                }}
              />
            </div>
          ) : (
            /* ── 视图 A：深度研究发起台（Launchpad） ── */
            <div className="mx-auto max-w-3xl py-2 space-y-5">
              {/* 错误或引导横幅 */}
              {error && (
                <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-xs text-destructive flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <AlertCircle className="size-4 shrink-0" />
                    <span>{error}</span>
                  </div>
                  {error.includes("模型配置") || error.includes("API Key") ? (
                    <button
                      type="button"
                      onClick={onGoSettings}
                      className="shrink-0 rounded-lg bg-gold px-3 py-1.5 text-xs font-medium text-[#17140a] hover:brightness-110 transition-all"
                    >
                      去配置 API Key →
                    </button>
                  ) : null}
                </div>
              )}

              {/* 第一步：研究范式选择 */}
              <div className="space-y-2.5">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-medium text-foreground/90 flex items-center gap-2">
                    <span className="inline-grid place-items-center size-4.5 rounded-full bg-gold/20 text-gold text-[10px] font-mono">1</span>
                    选择深度研究模板
                  </label>
                  <span className="text-[11px] text-muted-foreground">不同模板自动匹配对应的数据抓取管线与报告结构</span>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  {TEMPLATES.map((t) => {
                    const chosen = template === t.id;
                    const meta = TEMPLATE_META[t.id];
                    const Icon = meta.icon;
                    const chapters = SECTION_ORDER[t.id] || [];
                    return (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => {
                          setTemplate(t.id);
                          setError("");
                          if (t.id === "industry" && pickedTarget) {
                            setPickedTarget(null);
                            setTargetInput("");
                          }
                        }}
                        className={`text-left rounded-xl border p-3.5 transition-all ${
                          chosen
                            ? "border-gold bg-accent/60 shadow-sm"
                            : "border-border bg-card hover:border-gold/35 hover:bg-secondary/30"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <div className={`grid place-items-center size-7 rounded-lg ${chosen ? "bg-gold text-[#17140a]" : "bg-secondary text-muted-foreground"}`}>
                              <Icon className="size-4" />
                            </div>
                            <div>
                              <div className="flex items-center gap-1.5">
                                <span className={`text-sm font-medium ${chosen ? "text-gold" : "text-foreground"}`}>{t.label}</span>
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary/80 text-muted-foreground">{meta.tagline}</span>
                              </div>
                            </div>
                          </div>
                          <div className={`size-4 rounded-full border grid place-items-center ${chosen ? "border-gold bg-gold/20" : "border-border"}`}>
                            {chosen && <div className="size-2 rounded-full bg-gold" />}
                          </div>
                        </div>
                        <p className="mt-2 text-[11.5px] leading-relaxed text-muted-foreground">
                          {meta.detail}
                        </p>
                        <div className="mt-2.5 flex flex-wrap gap-1">
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-background/70 border border-border/60 text-muted-foreground">核心摘要</span>
                          {chapters.map((ch) => (
                            <span key={ch} className="text-[10px] px-1.5 py-0.5 rounded bg-background/70 border border-border/60 text-muted-foreground">
                              {ch}
                            </span>
                          ))}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* 第二步：研究标的输入 + 智能联想 */}
              <div className="rounded-xl border border-border bg-card p-4 space-y-4">
                <div className="space-y-2" ref={searchBoxRef}>
                  <label className="text-xs font-medium text-foreground/90 flex items-center gap-2">
                    <span className="inline-grid place-items-center size-4.5 rounded-full bg-gold/20 text-gold text-[10px] font-mono">2</span>
                    {template === "industry" ? "输入目标行业或赛道" : "输入目标股票 / ETF（支持代码、简称、拼音首字母）"}
                  </label>

                  <div className="relative">
                    <div className="flex items-center gap-2 rounded-lg bg-input border border-border focus-within:border-gold/60 px-3 py-2.5 transition-colors">
                      <Search className="size-4 text-gold shrink-0" />
                      <input
                        ref={targetInputRef}
                        value={targetInput}
                        onChange={(e) => {
                          setTargetInput(e.target.value);
                          setPickedTarget(null);
                          if (error) setError("");
                        }}
                        onFocus={() => {
                          if (candidates.length > 0 && template !== "industry") setDropdownOpen(true);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") {
                            setDropdownOpen(false);
                            return;
                          }
                          if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                            e.preventDefault();
                            void launchResearch({
                              rawTarget: pickedTarget ? pickedTarget.code : targetInput,
                              tpl: template,
                              queryText: focusQuery,
                              known: pickedTarget,
                            });
                          }
                        }}
                        placeholder={
                          template === "industry"
                            ? "输入行业名称，例如：半导体、光伏设备、白酒、人工智能…"
                            : "输入 600519、贵州茅台 或 gzmt，可从下拉列表点选精确标的…"
                        }
                        className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                        spellCheck={false}
                      />
                      {searchingStock && <Loader2 className="size-4 animate-spin text-muted-foreground shrink-0" />}
                      {pickedTarget && (
                        <span className="shrink-0 rounded bg-gold/15 border border-gold/35 px-2 py-0.5 text-[11px] font-mono text-gold">
                          已锁定 {pickedTarget.code}
                        </span>
                      )}
                    </div>

                    {/* 联想候选下拉框 */}
                    {dropdownOpen && candidates.length > 0 && template !== "industry" && (
                      <div className="absolute z-20 left-0 right-0 mt-1 rounded-xl border border-border bg-popover shadow-lg overflow-hidden">
                        {candidates.map((c) => (
                          <button
                            key={c.code}
                            type="button"
                            onClick={() => selectStockCandidate(c)}
                            className="w-full flex items-center justify-between px-3.5 py-2 text-xs hover:bg-accent transition-colors text-left"
                          >
                            <div className="flex items-center gap-2">
                              <span className="font-medium text-foreground">{c.name}</span>
                              {c.exchange && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">{c.exchange}</span>
                              )}
                            </div>
                            <span className="font-mono text-muted-foreground">{c.code}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* 快捷标的气泡 */}
                  <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                    <span className="text-[11px] text-muted-foreground mr-0.5">
                      {template === "industry" ? "热门赛道：" : recentStocks.length > 0 ? "最近浏览 / 常用：" : "示例标的："}
                    </span>
                    {template === "industry"
                      ? HOT_INDUSTRIES.map((ind) => (
                          <button
                            key={ind}
                            type="button"
                            onClick={() => {
                              setTargetInput(ind);
                              setPickedTarget(null);
                              setError("");
                            }}
                            className={`rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
                              targetInput.trim() === ind
                                ? "border-gold/50 bg-accent text-gold"
                                : "border-border/70 bg-secondary/40 text-muted-foreground hover:text-foreground"
                            }`}
                          >
                            {ind}
                          </button>
                        ))
                      : quickStocks.map((s) => {
                          const activeChip = pickedTarget?.code === s.code;
                          return (
                            <button
                              key={s.code}
                              type="button"
                              onClick={() => selectStockCandidate(s)}
                              className={`rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
                                activeChip
                                  ? "border-gold/50 bg-accent text-gold"
                                  : "border-border/70 bg-secondary/40 text-muted-foreground hover:text-foreground"
                              }`}
                            >
                              {s.name} <span className="font-mono opacity-70">{s.code.split(".")[0]}</span>
                            </button>
                          );
                        })}
                  </div>
                </div>

                {/* 第三步：可选研究关注点（打通后端 query） */}
                <div className="space-y-2 pt-2 border-t border-border/60">
                  <label className="text-xs font-medium text-foreground/90 flex items-center gap-2">
                    <span className="inline-grid place-items-center size-4.5 rounded-full bg-secondary text-muted-foreground text-[10px] font-mono">3</span>
                    特别关注的研究问题
                    <span className="text-[10.5px] font-normal text-muted-foreground">（可选 · AI 会在撰写各章节时重点回应）</span>
                  </label>

                  <input
                    value={focusQuery}
                    onChange={(e) => setFocusQuery(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                        e.preventDefault();
                        void launchResearch({
                          rawTarget: pickedTarget ? pickedTarget.code : targetInput,
                          tpl: template,
                          queryText: focusQuery,
                          known: pickedTarget,
                        });
                      }
                    }}
                    placeholder="例如：重点分析海外收入增速、毛利率拐点、自由现金流或分红可持续性…"
                    className="w-full rounded-lg bg-input border border-border focus:border-gold/60 px-3 py-2 text-xs outline-none placeholder:text-muted-foreground transition-colors"
                  />

                  <div className="flex flex-wrap gap-1.5">
                    {FOCUS_PRESETS[template].map((preset) => (
                      <button
                        key={preset}
                        type="button"
                        onClick={() => setFocusQuery((cur) => (cur === preset ? "" : preset))}
                        className={`rounded-md border px-2 py-0.5 text-[10.5px] transition-colors ${
                          focusQuery === preset
                            ? "border-gold/50 bg-accent text-gold"
                            : "border-transparent bg-secondary/40 text-muted-foreground hover:text-foreground"
                        }`}
                      >
                        + {preset}
                      </button>
                    ))}
                  </div>
                </div>

                {/* 启动主按钮 */}
                <div className="pt-2 flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    <Clock3 className="size-3.5 text-gold shrink-0" />
                    <span>预计 25–60 秒完成多源取数与逐章流式撰写，后台切页不中断</span>
                  </div>

                  <motion.button
                    whileTap={{ scale: 0.985 }}
                    type="button"
                    disabled={submitting || (!targetInput.trim() && !pickedTarget)}
                    onClick={() =>
                      void launchResearch({
                        rawTarget: pickedTarget ? pickedTarget.code : targetInput,
                        tpl: template,
                        queryText: focusQuery,
                        known: pickedTarget,
                      })
                    }
                    className="flex items-center gap-2 rounded-lg bg-gold px-5 py-2.5 text-sm font-medium text-[#17140a] hover:brightness-110 disabled:opacity-45 disabled:cursor-not-allowed transition-all shadow-sm"
                  >
                    {submitting ? (
                      <>
                        <Loader2 className="size-4 animate-spin" /> 正在核验标的并启动…
                      </>
                    ) : (
                      <>
                        <Play className="size-4 fill-current" /> 立即启动{templateLabel(template)}研究
                        <ArrowRight className="size-4" />
                      </>
                    )}
                  </motion.button>
                </div>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
