import { useConfirm } from "../ui/confirm";
import { isImeKeyEvent } from "../../lib/ime";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTheme } from "next-themes";
import {
  BookOpen, Brain, Cpu, Database, FlaskConical, Info, Loader2, MessagesSquare, Plus,
  RefreshCw, SlidersHorizontal, Trash2, UserRound, X,
} from "lucide-react";
import { Settings as ModelSettings } from "./Settings";
import { McpSettings, SchedulerSettings } from "../settings/IntegrationSettings";
import { TEMPLATES } from "../../data/report";
import {
  api, type AppSettings, type KnowledgeStats, type LearnedProfile, type UserProfile,
} from "../../api/client";

type TabId = "general" | "models" | "context" | "research" | "personal" | "data" | "scheduler" | "mcp" | "about";

const TABS: { id: TabId; label: string; icon: typeof Cpu }[] = [
  { id: "general", label: "通用", icon: SlidersHorizontal },
  { id: "models", label: "模型服务", icon: Cpu },
  { id: "context", label: "对话与上下文", icon: MessagesSquare },
  { id: "research", label: "研究", icon: FlaskConical },
  { id: "personal", label: "个性化", icon: UserRound },
  { id: "data", label: "数据与知识库", icon: Database },
  { id: "scheduler", label: "定时巡检", icon: RefreshCw },
  { id: "mcp", label: "MCP 工具服务", icon: BookOpen },
  { id: "about", label: "关于", icon: Info },
];

const DEFAULT_APP: AppSettings = {
  research_write_mode: "stream",
  default_template: "equity_deep",
  context_window_k: 0,
  compact_threshold_pct: 70,
  knowledge_enabled: true,
  learn_profile: true,
};

export const START_PAGE_KEY = "finresearch_start_page";

interface Props {
  active: { providerId: string; modelId: string } | null;
  onActivate: (p: { providerId: string; modelId: string; provider: string; model: string; accent: string }) => void;
}

/** 设置（v1.8.0，参考 Claude Desktop 的分栏设置）：左侧分组，右侧内容；改动即时保存。 */
export function SettingsPage({ active, onActivate }: Props) {
  const [tab, setTab] = useState<TabId>("general");
  const [app, setApp] = useState<AppSettings>(DEFAULT_APP);
  const [appError, setAppError] = useState("");
  const loadedRef = useRef(false);
  const saveTimer = useRef<number | null>(null);
  const pendingAppRef = useRef<AppSettings | null>(null);

  useEffect(() => {
    api.appSettings()
      .then((s) => { setApp({ ...DEFAULT_APP, ...s }); })
      .catch(() => {})
      .finally(() => { loadedRef.current = true; });
    return () => {
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
      if (pendingAppRef.current) {
        const next = pendingAppRef.current;
        pendingAppRef.current = null;
        api.saveAppSettings(next).catch(() => {});
      }
    };
  }, []);

  const patchApp = (p: Partial<AppSettings>) => {
    if (!loadedRef.current) return; // 首次读取完成前不写，避免默认值覆盖已保存的偏好
    const next = { ...app, ...p };
    setApp(next);
    pendingAppRef.current = next;
    // 立即广播给同窗口其他视图（如切换默认研究模板后立即返回对话台立即生效，不等 300ms 去抖）
    window.dispatchEvent(new CustomEvent("finresearch:app-settings", { detail: next }));
    // 去抖：滑块拖动会连发多次，只保存最后一次，避免请求乱序到达把旧值写回
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null;
      pendingAppRef.current = null;
      api.saveAppSettings(next)
        .then(() => {
          setAppError("");
          window.dispatchEvent(new CustomEvent("finresearch:app-settings", { detail: next }));
        })
        .catch((e) => setAppError(`设置保存失败：${e instanceof Error ? e.message : String(e)}`));
    }, 300);
  };

  return (
    <div className="h-full min-h-0 grid grid-cols-[160px_minmax(0,1fr)] overflow-hidden">
      <nav aria-label="设置分类" className="border-r border-border bg-sidebar/40 p-3 space-y-0.5 overflow-y-auto">
        <p className="px-2 pt-1 pb-3 text-[10px] tracking-[0.2em] text-muted-foreground/70 uppercase">设置</p>
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              type="button"
              aria-current={tab === t.id ? "page" : undefined}
              onClick={() => setTab(t.id)}
              className={`w-full flex items-center gap-2.5 whitespace-nowrap rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${tab === t.id ? "bg-sidebar-accent text-foreground" : "text-muted-foreground hover:text-foreground hover:bg-sidebar-accent/50"}`}
            >
              <Icon className={`size-4 shrink-0 ${tab === t.id ? "text-gold" : ""}`} /> {t.label}
            </button>
          );
        })}
      </nav>
      <div className="min-h-0 overflow-hidden">
        {appError && <div className="m-4 mb-0 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">{appError}</div>}
        {tab === "models" ? (
          <ModelSettings active={active} onActivate={onActivate} />
        ) : (
          <div className="h-full overflow-y-auto">
            <div className="max-w-2xl p-6 space-y-6">
              {tab === "general" && <GeneralTab />}
              {tab === "context" && <ContextTab app={app} patch={patchApp} />}
              {tab === "research" && <ResearchTab app={app} patch={patchApp} />}
              {tab === "personal" && <PersonalTab app={app} patch={patchApp} />}
              {tab === "data" && <DataTab />}
              {tab === "scheduler" && <SchedulerSettings />}
              {tab === "mcp" && <McpSettings />}
              {tab === "about" && <AboutTab />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── 基础组件 ─────────────────────────────────────────────
function Section({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      {desc && <p className="mt-1 text-xs text-muted-foreground leading-relaxed">{desc}</p>}
      <div className="mt-3 rounded-xl border border-border bg-card divide-y divide-border">{children}</div>
    </section>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3">
      <div className="min-w-0">
        <div className="text-sm text-foreground/90">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] text-muted-foreground leading-relaxed">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Toggle({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 rounded-full transition-colors disabled:opacity-50 ${checked ? "bg-gold" : "bg-secondary"}`}
    >
      <span className={`absolute top-0.5 size-4 rounded-full bg-white shadow transition-all ${checked ? "left-[18px]" : "left-0.5"}`} />
    </button>
  );
}

function Segmented<T extends string | number>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="flex rounded-lg bg-secondary/60 p-0.5 text-xs">
      {options.map((o) => (
        <button
          key={String(o.value)}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-2.5 py-1 transition-colors ${value === o.value ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ── 通用 ─────────────────────────────────────────────────
function GeneralTab() {
  const { theme, setTheme } = useTheme();
  const desktop = typeof window !== "undefined" ? window.finresearch?.app : undefined;
  const [prefs, setPrefs] = useState<{ closeToTray: boolean; autoLaunch: boolean } | null>(null);
  const [startPage, setStartPage] = useState<string>(() => {
    try { return localStorage.getItem(START_PAGE_KEY) || "overview"; } catch { return "overview"; }
  });

  useEffect(() => {
    desktop?.getPreferences().then((p) => setPrefs({ closeToTray: p.closeToTray, autoLaunch: p.autoLaunch })).catch(() => {});
  }, [desktop]);

  const updateDesktop = (p: Partial<{ closeToTray: boolean; autoLaunch: boolean }>) => {
    if (!desktop || !prefs) return;
    const next = { ...prefs, ...p };
    setPrefs(next);
    desktop.setPreferences(p).then((saved) => setPrefs({ closeToTray: saved.closeToTray, autoLaunch: saved.autoLaunch })).catch(() => {});
  };

  return (
    <>
      <Section title="外观">
        <Row label="主题">
          <Segmented
            value={(theme as string) || "system"}
            options={[{ value: "light", label: "浅色" }, { value: "dark", label: "深色" }, { value: "system", label: "跟随系统" }]}
            onChange={(v) => setTheme(v)}
          />
        </Row>
        <Row label="启动后打开">
          <Segmented
            value={startPage}
            options={[
              { value: "overview", label: "市场总览" },
              { value: "research", label: "深度研究" },
              { value: "chat", label: "投研问答" },
              { value: "market", label: "实时行情" },
              { value: "reports", label: "报告库" },
            ]}
            onChange={(v) => { setStartPage(v); try { localStorage.setItem(START_PAGE_KEY, v); } catch { /* ignore */ } }}
          />
        </Row>
      </Section>

      <Section title="桌面" desc={desktop ? "关闭窗口后景渊驻留在系统托盘，研究任务在后台继续运行；在托盘图标菜单中选择「退出」才会完全退出。" : "以下选项仅在桌面客户端中可用。"}>
        <Row label="关闭窗口时最小化到托盘" hint="关闭后点击托盘图标即可恢复窗口；关闭此项则关闭窗口即退出">
          <Toggle checked={prefs?.closeToTray ?? true} disabled={!desktop || !prefs} onChange={(v) => updateDesktop({ closeToTray: v })} />
        </Row>
        <Row label="开机自动启动" hint="登录系统后在后台启动（托盘驻留）">
          <Toggle checked={prefs?.autoLaunch ?? false} disabled={!desktop || !prefs} onChange={(v) => updateDesktop({ autoLaunch: v })} />
        </Row>
      </Section>
    </>
  );
}

// ── 对话与上下文 ─────────────────────────────────────────
function ContextTab({ app, patch }: { app: AppSettings; patch: (p: Partial<AppSettings>) => void }) {
  const presets = [0, 32, 64, 128, 200];
  return (
    <>
      <Section
        title="上下文窗口与自动压缩"
        desc="对话变长时，较早的内容会被模型写成一份交接摘要（用户目标、标的与关键数据、结论、未决问题、偏好），随后基于摘要 + 最近几轮继续，避免超出模型窗口报错。摘要同时存入本地知识库。"
      >
        <Row label="模型上下文窗口" hint="自动：按模型名推断（DeepSeek 64K、Qwen/Kimi/GLM 128K、Claude 200K…，未知按 64K）">
          <Segmented
            value={presets.includes(app.context_window_k) ? app.context_window_k : -1}
            options={[...presets.map((k) => ({ value: k, label: k === 0 ? "自动" : `${k}K` }))]}
            onChange={(v) => patch({ context_window_k: v < 0 ? 0 : v })}
          />
        </Row>
        <Row label={`自动压缩阈值：${app.compact_threshold_pct}%`} hint="上下文用量达到窗口的该比例时压缩；越低越省 token，越高保留原文越多">
          <input
            type="range" min={50} max={90} step={5}
            value={app.compact_threshold_pct}
            onChange={(e) => patch({ compact_threshold_pct: Number(e.target.value) })}
            className="w-40 accent-[var(--gold,#d4a843)]"
          />
        </Row>
      </Section>
      <Section title="知识检索">
        <Row label="允许 AI 检索本地知识库" hint="对话中提到之前的报告/研究/讨论时，AI 会先在本机知识库中检索（RAG），引用时注明报告日期">
          <Toggle checked={app.knowledge_enabled} onChange={(v) => patch({ knowledge_enabled: v })} />
        </Row>
      </Section>
    </>
  );
}

// ── 研究 ─────────────────────────────────────────────────
function ResearchTab({ app, patch }: { app: AppSettings; patch: (p: Partial<AppSettings>) => void }) {
  return (
    <Section title="深度研究" desc="研究在独立的「深度研究」工作台中发起与跟踪，完成后自动进入报告库与知识库，并可一键带上下文跳转至「投研问答」继续追问。">
      <Row label="默认研究模板">
        <Segmented
          value={app.default_template}
          options={TEMPLATES.map((t) => ({ value: t.id, label: t.label }))}
          onChange={(v) => patch({ default_template: v })}
        />
      </Row>
      <Row
        label="撰写方式"
        hint={app.research_write_mode === "stream"
          ? "单次流式（推荐）：一次请求从摘要写到最后一节，正文连续输出，思考型模型只思考一次"
          : "分段并行：每节单独请求、两节并行，适合不支持长输出的模型；节与节之间会有等待"}
      >
        <Segmented
          value={app.research_write_mode}
          options={[{ value: "stream", label: "单次流式" }, { value: "parallel", label: "分段并行" }]}
          onChange={(v) => patch({ research_write_mode: v })}
        />
      </Row>
    </Section>
  );
}

// ── 个性化 ───────────────────────────────────────────────
function PersonalTab({ app, patch }: { app: AppSettings; patch: (p: Partial<AppSettings>) => void }) {
  const confirm = useConfirm();
  const [profile, setProfile] = useState<UserProfile>({ risk_preference: "", investment_cycle: "", focus_metrics: "", analysis_style: "" });
  const profileLoaded = useRef(false);
  const pendingProfileRef = useRef<UserProfile | null>(null);
  const [profileErr, setProfileErr] = useState("");
  const [learned, setLearned] = useState<LearnedProfile | null>(null);
  const [note, setNote] = useState("");

  const loadLearned = () => api.learnedProfile().then(setLearned).catch(() => {});
  useEffect(() => {
    api.userProfile()
      .then((r) => { if (r.payload) setProfile(r.payload); })
      .catch(() => {})
      .finally(() => { profileLoaded.current = true; });
    void loadLearned();
    return () => {
      if (pendingProfileRef.current) {
        const p = pendingProfileRef.current;
        pendingProfileRef.current = null;
        api.saveUserProfile(p).catch(() => {});
      }
    };
  }, []);
  useEffect(() => {
    if (!profileLoaded.current) return;
    pendingProfileRef.current = profile;
    const t = window.setTimeout(() => {
      pendingProfileRef.current = null;
      api.saveUserProfile(profile)
        .then(() => setProfileErr(""))
        .catch((e) => setProfileErr(`偏好保存失败：${e instanceof Error ? e.message : String(e)}`));
    }, 400);
    return () => window.clearTimeout(t);
  }, [profile]);

  const field = (key: keyof UserProfile, label: string, placeholder: string) => (
    <label className="block">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <input
        value={profile[key] || ""}
        onChange={(e) => setProfile((p) => ({ ...p, [key]: e.target.value }))}
        placeholder={placeholder}
        className="mt-1 w-full rounded-lg bg-input border border-border px-3 py-2 text-xs outline-none focus:border-gold/40"
      />
    </label>
  );

  return (
    <>
      <Section title="我的研究偏好" desc="手动填写，作为对话与研究的上下文侧重（不会改变事实与数字）。留空的项按「稳健研究型」默认。">
        {profileErr && <div className="px-4 pt-3 text-xs text-destructive">{profileErr}</div>}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-4">
          {field("risk_preference", "风险偏好", "如 稳健")}
          {field("investment_cycle", "投资周期", "如 中期")}
          {field("focus_metrics", "关注指标", "如 ROE、现金流")}
          {field("analysis_style", "分析风格", "如 结论先行")}
        </div>
      </Section>

      <Section title="AI 学习的偏好" desc="景渊会从你的使用中逐步了解你：常看的标的、常用的研究类型、常问的话题，以及你在对话中明确表达的偏好（由 AI 记为笔记）。全部只保存在本机。">
        <Row label="从使用中学习偏好" hint="关闭后不再记录行为与笔记，也不再注入已学习的偏好">
          <Toggle checked={app.learn_profile} onChange={(v) => patch({ learn_profile: v })} />
        </Row>
        <div className="px-4 py-3 space-y-2 text-xs">
          <LearnedLine icon={<Brain className="size-3.5" />} label="常关注标的" items={learned?.stocks.map((s) => `${s.label}×${s.count}`)} />
          <LearnedLine icon={<FlaskConical className="size-3.5" />} label="常用研究类型" items={learned?.templates.map((t) => `${t.label}×${t.count}`)} />
          <LearnedLine icon={<MessagesSquare className="size-3.5" />} label="常问话题" items={learned?.topics.map((t) => `${t.key}×${t.count}`)} />
        </div>
        <div className="px-4 py-3">
          <div className="text-[11px] text-muted-foreground mb-2">偏好笔记</div>
          <div className="space-y-1.5">
            {(learned?.notes ?? []).length === 0 && <div className="text-xs text-muted-foreground/70">暂无。对话中说「我更看重分红」之类的偏好，AI 会自动记下。</div>}
            {(learned?.notes ?? []).map((n) => (
              <div key={n.id} className="flex items-center gap-2 text-xs">
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${n.source === "user" ? "bg-secondary text-muted-foreground" : "bg-accent text-gold"}`}>{n.source === "user" ? "手动" : "AI"}</span>
                <span className="flex-1 text-foreground/85">{n.text}</span>
                <button onClick={() => api.deleteProfileNote(n.id).then(loadLearned).catch(() => {})} className="text-muted-foreground hover:text-destructive"><X className="size-3.5" /></button>
              </div>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !isImeKeyEvent(e.nativeEvent) && note.trim()) api.addProfileNote(note.trim()).then(() => { setNote(""); void loadLearned(); }).catch(() => {}); }}
              placeholder="手动添加一条偏好，如：偏好高股息低估值"
              className="flex-1 rounded-lg bg-input border border-border px-3 py-1.5 text-xs outline-none focus:border-gold/40"
            />
            <button
              disabled={!note.trim()}
              onClick={() => api.addProfileNote(note.trim()).then(() => { setNote(""); void loadLearned(); }).catch(() => {})}
              className="flex items-center gap-1 rounded-lg bg-secondary px-2.5 py-1.5 text-xs hover:bg-secondary/70 disabled:opacity-50"
            >
              <Plus className="size-3.5" /> 添加
            </button>
          </div>
        </div>
        <Row label="清除学习数据" hint="删除全部行为记录与偏好笔记（含行情搜索记录），不可恢复">
          <button
            onClick={async () => { if (await confirm("清除全部学习数据？")) void api.clearLearnedProfile().then(loadLearned).catch(() => {}); }}
            className="flex items-center gap-1 rounded-lg bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive hover:bg-destructive/20"
          >
            <Trash2 className="size-3.5" /> 清除
          </button>
        </Row>
      </Section>
    </>
  );
}

function LearnedLine({ icon, label, items }: { icon: ReactNode; label: string; items?: string[] }) {
  return (
    <div className="flex items-start gap-2">
      <span className="flex items-center gap-1.5 text-muted-foreground shrink-0 w-24">{icon}{label}</span>
      <span className="text-foreground/85">{items && items.length ? items.join("、") : <span className="text-muted-foreground/60">暂无</span>}</span>
    </div>
  );
}

// ── 数据与知识库 ─────────────────────────────────────────
function DataTab() {
  const confirm = useConfirm();
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  useEffect(() => { api.knowledgeStats().then(setStats).catch(() => {}); }, []);

  return (
    <>
      <Section title="本地知识库" desc="研究生成的每一份报告都会自动切分入库，对话中的 AI 通过检索（RAG）引用它们；较早对话的压缩摘要也会入库，形成跨会话记忆。">
        <Row label="已入库" hint={stats?.updated_at ? `最近更新 ${stats.updated_at}` : undefined}>
          <span className="text-xs text-foreground/85 tabular">
            {stats ? `${stats.reports} 份报告 · ${stats.conversations} 段对话摘要${stats.external ? ` · ${stats.external} 份外部文档` : ""} · ${stats.chunks} 个片段` : "读取中…"}
          </span>
        </Row>
        <Row label="重建报告索引" hint="从报告库重新切分并建立全文索引（报告很多时需要几秒）">
          <button
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setMsg("");
              api.knowledgeReindex()
                .then((r) => { setStats(r); setMsg(`已重建：${r.reports} 份报告`); })
                .catch((e) => setMsg(`重建失败：${e instanceof Error ? e.message : String(e)}`))
                .finally(() => setBusy(false));
            }}
            className="flex items-center gap-1 rounded-lg bg-secondary px-2.5 py-1.5 text-xs hover:bg-secondary/70 disabled:opacity-50"
          >
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} 重建
          </button>
        </Row>
        {msg && <div className="px-4 py-2 text-[11px] text-muted-foreground">{msg}</div>}
      </Section>
      <Section title="本地数据">
        <Row label="行情搜索记录">
          <button
            onClick={async () => { if (await confirm("清除全部行情搜索记录？")) void api.clearMarketHistory().catch(() => {}); }}
            className="flex items-center gap-1 rounded-lg bg-secondary px-2.5 py-1.5 text-xs hover:bg-secondary/70"
          >
            <Trash2 className="size-3.5" /> 清除
          </button>
        </Row>
        <Row label="数据位置" hint="对话、报告、知识库和加密密钥保存在运行景渊的设备上。桌面安装版使用系统用户数据目录，便携版使用应用旁的 portable-data，Docker 使用持久卷。">
          <Database className="size-4 text-muted-foreground" />
        </Row>
      </Section>
    </>
  );
}

// ── 关于 ─────────────────────────────────────────────────
function AboutTab() {
  const [version, setVersion] = useState("");
  useEffect(() => {
    window.finresearch?.app?.getPreferences().then((p) => setVersion(p.version || "")).catch(() => {});
  }, []);
  return (
    <Section title="景渊 JingYuan · 金融研究助手">
      <Row label="版本"><span className="text-xs tabular text-foreground/85">{version || "2.0.0"}</span></Row>
      <Row label="数据原则" hint="只读研究，永不接入交易；所有数值由本地工具库计算并可溯源，AI 仅供研究参考，不构成投资建议。">
        <BookOpen className="size-4 text-gold" />
      </Row>
    </Section>
  );
}
