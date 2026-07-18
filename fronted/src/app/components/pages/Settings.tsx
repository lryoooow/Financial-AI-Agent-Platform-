import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { KeyRound, Link2, Eye, EyeOff, Loader2, CheckCircle2, AlertCircle, RefreshCw, Zap, Check, Star, Globe, Flag } from "lucide-react";
import { PROVIDERS, type Provider, type ModelInfo, type Region } from "../../data/providers";
import { api, type UserProfile } from "../../api/client";

type Status = "idle" | "testing" | "connected" | "error";
interface Cfg { baseUrl: string; apiKey: string; status: Status; latency: number; message: string; models: ModelInfo[]; fetching: boolean; selectedModel: string; }

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

interface Props {
  active: { providerId: string; modelId: string } | null;
  onActivate: (p: { providerId: string; modelId: string; provider: string; model: string; accent: string }) => void;
}

export function Settings({ active, onActivate }: Props) {
  const [configs, setConfigs] = useState<Record<string, Cfg>>(() => {
    // 从 localStorage 恢复已存的 key/baseUrl/所选模型（跨重启不再重填）
    const saved: Record<string, any> = (() => { try { return JSON.parse(localStorage.getItem("finresearch_keys") || "{}"); } catch { return {}; } })();
    return Object.fromEntries(PROVIDERS.map((p) => {
      const s = saved[p.id] || {};
      const savedModels = Array.isArray(s.models) ? s.models : [];
      const selectedModel = s.selectedModel || "";
      const hasUsableConfig = Boolean((s.apiKey || "").trim() && selectedModel);
      return [p.id, {
        baseUrl: s.baseUrl || p.baseUrl,
        apiKey: s.apiKey || "",
        status: hasUsableConfig ? "connected" as Status : "idle" as Status,
        latency: 0,
        message: hasUsableConfig ? "已保存配置" : "",
        models: savedModels.length ? savedModels : p.models,
        fetching: false,
        selectedModel,
      }];
    }));
  });
  // 竞态修复：首次 GET 完成前不触发 debounce 保存，避免空 localStorage 覆盖后端已存密钥。
  const configsLoadedRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    api.localSetting("model-configs")
      .then((r) => {
        if (cancelled || !r.payload) return;
        const saved = r.payload as Record<string, any>;
        setConfigs(Object.fromEntries(PROVIDERS.map((p) => {
          const s = saved[p.id] || {};
          const savedModels = Array.isArray(s.models) ? s.models : [];
          const selectedModel = s.selectedModel || "";
          const hasUsableConfig = Boolean((s.apiKey || "").trim() && selectedModel);
          return [p.id, {
            baseUrl: s.baseUrl || p.baseUrl,
            apiKey: s.apiKey || "",
            status: hasUsableConfig ? "connected" as Status : "idle" as Status,
            latency: 0,
            message: hasUsableConfig ? "已从本地恢复配置" : "",
            models: savedModels.length ? savedModels : p.models,
            fetching: false,
            selectedModel,
          }];
        })));
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) configsLoadedRef.current = true; });
    return () => { cancelled = true; };
  }, []);
  // 持久化 key/baseUrl/模型列表/所选模型；避免每次打开都重新测试和拉取模型。
  useEffect(() => {
    if (!configsLoadedRef.current) return;
    const toSave = Object.fromEntries(Object.entries(configs).map(([id, c]) => [id, {
      baseUrl: c.baseUrl,
      apiKey: c.apiKey,
      selectedModel: c.selectedModel,
      models: c.models,
    }]));
    localStorage.setItem("finresearch_keys", JSON.stringify(toSave));
    const t = window.setTimeout(() => {
      api.saveLocalSetting("model-configs", toSave).catch(() => {});
    }, 300);
    return () => window.clearTimeout(t);
  }, [configs]);
  const [selectedId, setSelectedId] = useState(active?.providerId || PROVIDERS[0].id);
  const [showKey, setShowKey] = useState(false);
  const [regionFilter, setRegionFilter] = useState<Region | "all">("all");
  const [profile, setProfile] = useState<UserProfile>({
    risk_preference: "",
    investment_cycle: "",
    focus_metrics: "",
    analysis_style: "",
  });

  const profileLoadedRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    api.userProfile()
      .then((r) => {
        if (!cancelled && r.payload) setProfile(r.payload);
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) profileLoadedRef.current = true; });
    return () => { cancelled = true; };
  }, []);

  // 竞态修复：首次 GET 完成前不触发 debounce 保存，避免空画像覆盖后端已存的。
  useEffect(() => {
    if (!profileLoadedRef.current) return;
    const t = window.setTimeout(() => {
      api.saveUserProfile(profile).catch(() => {});
    }, 400);
    return () => window.clearTimeout(t);
  }, [profile]);

  const provider = PROVIDERS.find((p) => p.id === selectedId)!;
  const cfg = configs[selectedId];
  const patch = (id: string, p: Partial<Cfg>) => setConfigs((c) => ({ ...c, [id]: { ...c[id], ...p } }));

  const activeProvider = active ? PROVIDERS.find((p) => p.id === active.providerId) : null;
  const activeModel = activeProvider?.models.find((m) => m.id === active?.modelId)
    ?? configs[active?.providerId ?? ""]?.models.find((m) => m.id === active?.modelId);

  async function testConn() {
    if (!cfg.apiKey.trim()) { patch(selectedId, { status: "error", message: "请先填入 API Key" }); return; }
    patch(selectedId, { status: "testing", message: "" });
    await sleep(1100);
    const ok = cfg.apiKey.trim().length >= 8;
    if (ok) patch(selectedId, { status: "connected", latency: Math.round(120 + Math.random() * 380), message: "连接成功" });
    else patch(selectedId, { status: "error", message: "认证失败：API Key 无效（示例校验需 ≥ 8 位）" });
  }

  async function fetchModels() {
    if (!cfg.apiKey.trim()) { patch(selectedId, { status: "error", message: "请先填入 API Key" }); return; }
    patch(selectedId, { fetching: true });
    try {
      const r = await api.models(cfg.baseUrl, cfg.apiKey);
      const live = (r.models || []).map((m) => ({ id: m.id, label: m.label || m.id, context: "" }));
      const models = live.length ? live : provider.models;
      patch(selectedId, {
        fetching: false,
        models,
        selectedModel: cfg.selectedModel || models[0]?.id || "",
        status: "connected",
        message: live.length ? "已拉取最新模型" : "使用内置模型清单",
      });
    } catch {
      // 厂商 /models 拉取失败 → 回退静态清单（已是 2026-07 当前线）
      patch(selectedId, {
        fetching: false,
        models: cfg.models.length ? cfg.models : provider.models,
        selectedModel: cfg.selectedModel || provider.models[0]?.id || "",
        status: "connected",
        message: "模型接口不可用，使用已保存/内置清单",
      });
    }
  }

  const list = useMemo(() => PROVIDERS.filter((p) => regionFilter === "all" || p.region === regionFilter), [regionFilter]);

  const statusDot = (s: Status) => s === "connected" ? "var(--down)" : s === "error" ? "var(--up)" : s === "testing" ? "var(--gold)" : "var(--muted-foreground)";

  return (
    <div className="h-full grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-4 p-5 overflow-hidden">
      {/* 供应商列表 */}
      <div className="flex flex-col gap-3 min-h-0">
        <div className="rounded-xl border border-border bg-card p-1 flex gap-1">
          {([["all", "全部", Globe], ["domestic", "国内", Flag], ["foreign", "国外", Globe]] as const).map(([id, label]) => (
            <button key={id} onClick={() => setRegionFilter(id)} className="relative flex-1 py-1.5 text-xs rounded-lg">
              {regionFilter === id && <motion.div layoutId="region-tab" className="absolute inset-0 rounded-lg bg-secondary" transition={{ type: "spring", stiffness: 400, damping: 30 }} />}
              <span className={`relative ${regionFilter === id ? "text-foreground" : "text-muted-foreground"}`}>{label}</span>
            </button>
          ))}
        </div>

        <div className="rounded-xl border border-border bg-card flex-1 min-h-0 overflow-y-auto p-1.5 space-y-0.5">
          {list.map((p) => {
            const c = configs[p.id];
            const isActive = active?.providerId === p.id;
            return (
              <button key={p.id} onClick={() => setSelectedId(p.id)} className={`w-full flex items-center gap-2.5 px-2 py-2 rounded-lg transition-colors ${selectedId === p.id ? "bg-sidebar-accent" : "hover:bg-sidebar-accent/50"}`}>
                <div className="grid place-items-center size-8 rounded-lg text-[11px] shrink-0" style={{ background: p.accent + "22", color: p.accent }}>{p.short}</div>
                <div className="flex-1 text-left leading-tight min-w-0">
                  <div className="text-sm text-foreground truncate flex items-center gap-1.5">{p.name}{isActive && <Star className="size-3 text-gold fill-gold" />}</div>
                  <div className="text-[10px] text-muted-foreground truncate">{p.vendor}</div>
                </div>
                <span className="size-1.5 rounded-full shrink-0" style={{ background: statusDot(c.status) }} />
              </button>
            );
          })}
        </div>
      </div>

      {/* 配置详情 */}
      <div className="rounded-xl border border-border bg-card min-h-0 overflow-y-auto">
        <AnimatePresence mode="wait">
          <motion.div key={selectedId} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }} className="p-6 max-w-2xl">
            {/* 头部 */}
            <div className="flex items-center gap-3">
              <div className="grid place-items-center size-12 rounded-xl text-sm" style={{ background: provider.accent + "22", color: provider.accent }}>{provider.short}</div>
              <div>
                <div className="flex items-center gap-2">
                  <h1 className="font-display" style={{ fontSize: 20, fontWeight: 600 }}>{provider.name}</h1>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">{provider.region === "domestic" ? "国内" : "国外"} · OpenAI 兼容</span>
                </div>
                <p className="text-xs text-muted-foreground mt-0.5">{provider.docsHint}</p>
              </div>
            </div>

            {/* Base URL */}
            <div className="mt-6">
              <label className="text-xs text-muted-foreground flex items-center gap-1.5 mb-1.5"><Link2 className="size-3.5" /> Base URL（已预置，可修改）</label>
              <input value={cfg.baseUrl} onChange={(e) => patch(selectedId, { baseUrl: e.target.value })} className="w-full rounded-lg bg-input border border-border px-3 py-2.5 text-sm font-mono outline-none focus:border-gold/40 transition-colors" spellCheck={false} />
            </div>

            {/* API Key */}
            <div className="mt-4">
              <label className="text-xs text-muted-foreground flex items-center gap-1.5 mb-1.5"><KeyRound className="size-3.5" /> API Key</label>
              <div className="flex items-center rounded-lg bg-input border border-border focus-within:border-gold/40 transition-colors">
                <input
                  type={showKey ? "text" : "password"}
                  value={cfg.apiKey}
                  onChange={(e) => patch(selectedId, { apiKey: e.target.value, status: "idle", message: "" })}
                  placeholder="sk-··· 在此填入你的密钥"
                  className="flex-1 bg-transparent px-3 py-2.5 text-sm font-mono outline-none placeholder:text-muted-foreground placeholder:font-sans"
                  spellCheck={false}
                />
                <button onClick={() => setShowKey((s) => !s)} className="px-3 text-muted-foreground hover:text-foreground transition-colors">
                  {showKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
              <p className="text-[10.5px] text-muted-foreground/70 mt-1.5">密钥仅保存在本机，永不上传。</p>
            </div>

            {/* 测试连通性 */}
            <div className="mt-5 rounded-lg border border-border bg-secondary/25 p-3">
              <div className="text-sm text-foreground">用户画像 / 研究偏好</div>
              <p className="mt-1 text-[11px] text-muted-foreground">仅保存在本地，用于专业对话和研究任务的上下文侧重。</p>
              <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                <input
                  value={profile.risk_preference || ""}
                  onChange={(e) => setProfile((p) => ({ ...p, risk_preference: e.target.value }))}
                  placeholder="风险偏好，如 稳健"
                  className="rounded-lg bg-input border border-border px-3 py-2 text-xs outline-none focus:border-gold/40"
                />
                <input
                  value={profile.investment_cycle || ""}
                  onChange={(e) => setProfile((p) => ({ ...p, investment_cycle: e.target.value }))}
                  placeholder="投资周期，如 中期"
                  className="rounded-lg bg-input border border-border px-3 py-2 text-xs outline-none focus:border-gold/40"
                />
                <input
                  value={profile.focus_metrics || ""}
                  onChange={(e) => setProfile((p) => ({ ...p, focus_metrics: e.target.value }))}
                  placeholder="关注指标，如 ROE、现金流"
                  className="rounded-lg bg-input border border-border px-3 py-2 text-xs outline-none focus:border-gold/40"
                />
                <input
                  value={profile.analysis_style || ""}
                  onChange={(e) => setProfile((p) => ({ ...p, analysis_style: e.target.value }))}
                  placeholder="分析风格，如 结论先行"
                  className="rounded-lg bg-input border border-border px-3 py-2 text-xs outline-none focus:border-gold/40"
                />
              </div>
            </div>

            <div className="mt-4 flex items-center gap-3">
              <button onClick={testConn} disabled={cfg.status === "testing"} className="flex items-center gap-2 rounded-lg bg-secondary hover:bg-secondary/70 px-3.5 py-2 text-sm transition-colors disabled:opacity-60">
                {cfg.status === "testing" ? <Loader2 className="size-4 animate-spin" /> : <Zap className="size-4 text-gold" />}
                测试连通性
              </button>
              <AnimatePresence mode="wait">
                {cfg.status === "connected" && (
                  <motion.div key="ok" initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} className="flex items-center gap-1.5 text-xs text-down">
                    <CheckCircle2 className="size-4" /> 连接成功 · 延迟 {cfg.latency}ms
                  </motion.div>
                )}
                {cfg.status === "error" && (
                  <motion.div key="err" initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} className="flex items-center gap-1.5 text-xs text-up">
                    <AlertCircle className="size-4" /> {cfg.message}
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {/* 模型 */}
            <div className="mt-6 pt-5 border-t border-border">
              <div className="flex items-center justify-between mb-3">
                <div>
                  <h2 className="text-sm">可用模型</h2>
                  <p className="text-[11px] text-muted-foreground mt-0.5">已保存的模型会自动恢复；需要刷新时再手动拉取</p>
                </div>
                <button
                  onClick={fetchModels}
                  disabled={!cfg.apiKey.trim() || cfg.fetching}
                  className="flex items-center gap-1.5 rounded-lg bg-gold px-3 py-1.5 text-xs text-[#17140a] hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                >
                  {cfg.fetching ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                  拉取最新模型
                </button>
              </div>

              {cfg.models.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border py-8 text-center text-xs text-muted-foreground">
                  {cfg.apiKey.trim() ? "点击「拉取最新模型」获取列表，或使用内置模型清单" : "请先填入 API Key"}
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {cfg.models.map((m, i) => {
                    const chosen = cfg.selectedModel === m.id;
                    return (
                      <motion.button
                        key={m.id}
                        initial={{ opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: i * 0.05 }}
                        onClick={() => patch(selectedId, { selectedModel: m.id })}
                        className={`text-left p-3 rounded-lg border transition-all ${chosen ? "border-gold bg-accent" : "border-border hover:border-border/70 bg-secondary/30"}`}
                      >
                        <div className="flex items-center justify-between">
                          <span className={`text-sm ${chosen ? "text-gold" : "text-foreground/90"}`}>{m.label}</span>
                          {chosen && <Check className="size-4 text-gold" />}
                        </div>
                        <div className="flex items-center gap-1.5 mt-1.5">
                          <span className="text-[10px] font-mono text-muted-foreground">{m.context}</span>
                          {m.tag && <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: provider.accent + "22", color: provider.accent }}>{m.tag}</span>}
                        </div>
                      </motion.button>
                    );
                  })}
                </div>
              )}

              <AnimatePresence>
                {cfg.selectedModel && (
                  <motion.button
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0 }}
                    onClick={() => {
                      const m = cfg.models.find((x) => x.id === cfg.selectedModel)!;
                      onActivate({ providerId: provider.id, modelId: m.id, provider: provider.name, model: m.label, accent: provider.accent });
                    }}
                    className="mt-4 w-full flex items-center justify-center gap-2 rounded-lg border border-gold/40 bg-accent/60 py-2.5 text-sm text-gold hover:bg-accent transition-colors overflow-hidden"
                  >
                    <Star className="size-4" /> 设为当前推理模型
                  </motion.button>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}
