import { isImeKeyEvent } from "../../lib/ime";
import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { KeyRound, Link2, Eye, EyeOff, Loader2, CheckCircle2, AlertCircle, RefreshCw, Zap, Check, Star, Globe, Flag, Plus, X, Gauge } from "lucide-react";
import { PROVIDERS, type Provider, type ModelInfo, type Region } from "../../data/providers";
import { api } from "../../api/client";

type Status = "idle" | "testing" | "connected" | "error";
interface Cfg { baseUrl: string; apiKey: string; reasoningEffort: string; fallbackModelsText: string; status: Status; latency: number; message: string; models: ModelInfo[]; fetching: boolean; selectedModel: string; }

const MASKED_KEY_RE = /^•{8}(?:.{4})?$/;

function isMaskedApiKey(k: string): boolean {
  return MASKED_KEY_RE.test(k || "");
}

function maskKeyForCache(k: string): string {
  const v = (k || "").trim();
  if (!v) return "";
  if (isMaskedApiKey(v)) return v;
  return v.length <= 6 ? "••••••••" : `••••••••${v.slice(-4)}`;
}

function normalizeApiKeyInput(prev: string, next: string): string {
  if (!isMaskedApiKey(prev) || !next.includes("•")) return next;
  // 防止在掩码中间误触单次 Backspace/Delete 瞬间清空整串已存密钥；
  // 全选删除（next === ""）、粘贴新密钥或点击右侧「更换」按钮仍可正常替换。
  if (next.length < prev.length) return next.length === 0 ? "" : prev;
  if (next.length === prev.length) return prev;
  let p = 0;
  while (p < prev.length && p < next.length && prev[p] === next[p]) p++;
  let s = 0;
  while (s < prev.length - p && s < next.length - p && prev[prev.length - 1 - s] === next[next.length - 1 - s]) s++;
  return next.slice(p, next.length - s);
}

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
      // v1.6.4：区分「从未保存过清单」（回退预置）与「保存过但被删空」（尊重空清单，
      // 否则预设厂商删光模型后静态清单立即"复活"，删除形同虚设）
      const models = Array.isArray(s.models) ? s.models : p.models;
      const selectedModel = s.selectedModel || "";
      const hasUsableConfig = Boolean((s.apiKey || "").trim() && selectedModel);
      return [p.id, {
        baseUrl: s.baseUrl || p.baseUrl,
        apiKey: s.apiKey || "",
        reasoningEffort: s.reasoningEffort || "",
        fallbackModelsText: Array.isArray(s.fallbackModels) ? s.fallbackModels.join(", ") : "",
        status: hasUsableConfig ? "connected" as Status : "idle" as Status,
        latency: 0,
        message: hasUsableConfig ? "已保存配置" : "",
        models,
        fetching: false,
        selectedModel,
      }];
    }));
  });
  // 竞态修复：首次 GET 完成前不触发 debounce 保存，避免空 localStorage 覆盖后端已存密钥；
  // 同时标记远端恢复本身不触发无意义的立即回写 PUT。
  const configsLoadedRef = useRef(false);
  const skipPersistOnceRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    api.localSetting("model-configs")
      .then((r) => {
        if (cancelled || !r.payload) return;
        const saved = r.payload as Record<string, any>;
        skipPersistOnceRef.current = true;
        setConfigs(Object.fromEntries(PROVIDERS.map((p) => {
          const s = saved[p.id] || {};
          // v1.6.4：同上 —— 保存过的空清单不被预置清单复活
          const models = Array.isArray(s.models) ? s.models : p.models;
          const selectedModel = s.selectedModel || "";
          const hasUsableConfig = Boolean((s.apiKey || "").trim() && selectedModel);
          return [p.id, {
            baseUrl: s.baseUrl || p.baseUrl,
            apiKey: s.apiKey || "",
            reasoningEffort: s.reasoningEffort || "",
        fallbackModelsText: Array.isArray(s.fallbackModels) ? s.fallbackModels.join(", ") : "",
            status: hasUsableConfig ? "connected" as Status : "idle" as Status,
            latency: 0,
            message: hasUsableConfig ? "已从本地恢复配置" : "",
            models,
            fetching: false,
            selectedModel,
          }];
        })));
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) configsLoadedRef.current = true; });
    return () => { cancelled = true; };
  }, []);
  // 持久化 key/baseUrl/模型列表/所选模型；localStorage 仅存掩码，真实密钥只发后端 DPAPI 加密落库。
  const pendingSaveRef = useRef<Record<string, any> | null>(null);
  useEffect(() => {
    const flushPending = () => {
      if (!pendingSaveRef.current) return;
      const payload = pendingSaveRef.current;
      pendingSaveRef.current = null;
      api.saveLocalSetting("model-configs", payload).catch(() => {});
    };
    window.addEventListener("beforeunload", flushPending);
    return () => {
      window.removeEventListener("beforeunload", flushPending);
      flushPending();
    };
  }, []);

  useEffect(() => {
    if (!configsLoadedRef.current) return;
    const toCache = Object.fromEntries(Object.entries(configs).map(([id, c]) => [id, {
      baseUrl: c.baseUrl,
      apiKey: maskKeyForCache(c.apiKey),
      reasoningEffort: c.reasoningEffort,
      fallbackModels: [...new Set(c.fallbackModelsText.split(/[,，]+/).map((id) => id.trim()).filter(Boolean))].slice(0, 4),
      selectedModel: c.selectedModel,
      models: c.models,
    }]));
    try {
      localStorage.setItem("finresearch_keys", JSON.stringify(toCache));
    } catch { /* ignore */ }
    if (skipPersistOnceRef.current) {
      skipPersistOnceRef.current = false;
      window.dispatchEvent(new CustomEvent("finresearch:configs-updated"));
      return;
    }
    const toSave = Object.fromEntries(Object.entries(configs).map(([id, c]) => [id, {
      baseUrl: c.baseUrl,
      apiKey: c.apiKey,
      reasoningEffort: c.reasoningEffort,
      fallbackModels: [...new Set(c.fallbackModelsText.split(/[,，]+/).map((id) => id.trim()).filter(Boolean))].slice(0, 4),
      selectedModel: c.selectedModel,
      models: c.models,
    }]));
    // v1.6.2：广播配置更新 —— 已挂载的 ModelPicker 据此即时刷新
    window.dispatchEvent(new CustomEvent("finresearch:configs-updated"));
    pendingSaveRef.current = toSave;
    const t = window.setTimeout(() => {
      pendingSaveRef.current = null;
      // v1.6.3（C1）：保存失败绝不静默 —— DB 是后端 overlay 的事实源
      api.saveLocalSetting("model-configs", toSave).catch((e) => {
        const msg = `密钥保存失败（${e instanceof Error ? e.message : String(e)}）。请检查后端连接后重试，否则换 key 不会生效。`;
        setSaveError(msg);
        console.error("[Settings] model-configs save failed:", e);
      });
    }, 300);
    return () => window.clearTimeout(t);
  }, [configs]);
  // v1.6.3（C1）：保存失败横幅（用户再编辑任一配置时清除）
  const [saveError, setSaveError] = useState("");
  // v1.7.0：合并 ModelPicker 快捷切档的外部写入。评审实证：App 的四个页面**常驻挂载**
  //（hidden 切页，非互斥卸载）——聊天页下拉切了思考深度后，本组件内存里的
  // reasoningEffort 仍是挂载时的旧值，用户再到设置页编辑任一字段就会整份回写、
  // 把档位静默覆盖回去。监听 configs-updated 只合并 reasoningEffort 字段；
  // 等值时返回原引用防广播回环（自己的保存也会触发本监听）。
  useEffect(() => {
    const mergeExternalEffort = () => {
      let saved: Record<string, any>;
      try {
        saved = JSON.parse(localStorage.getItem("finresearch_keys") || "{}");
      } catch {
        return;
      }
      setConfigs((c) => {
        let changed = false;
        const next: Record<string, Cfg> = { ...c };
        for (const [id, rec] of Object.entries(saved)) {
          const cur = next[id];
          if (!cur) continue;
          const eff = (rec as any)?.reasoningEffort ?? "";
          if (cur.reasoningEffort !== eff) {
            next[id] = { ...cur, reasoningEffort: eff };
            changed = true;
          }
        }
        return changed ? next : c;
      });
    };
    window.addEventListener("finresearch:configs-updated", mergeExternalEffort);
    return () => window.removeEventListener("finresearch:configs-updated", mergeExternalEffort);
  }, []);
  // v1.6.0（审计 C4）：selectedId 兜底 —— 存量记录里的 providerId 不在清单时回落首个，
  // 否则 configs[selectedId] 为 undefined，下方 value={cfg.baseUrl} 直接 TypeError 白屏
  const [selectedId, setSelectedId] = useState(
    () => (active?.providerId && PROVIDERS.some((p) => p.id === active.providerId) ? active.providerId : PROVIDERS[0].id),
  );
  useEffect(() => {
    if (!PROVIDERS.some((p) => p.id === selectedId)) setSelectedId(PROVIDERS[0].id);
  }, [selectedId]);
  // 外部（如聊天页 ModelPicker）切换了当前激活模型时，同步该厂商的 selectedModel 高亮
  useEffect(() => {
    if (!active?.providerId || !active?.modelId) return;
    setConfigs((c) => {
      const cur = c[active.providerId];
      if (!cur || cur.selectedModel === active.modelId) return c;
      if (!cur.models.some((m) => m.id === active.modelId)) return c;
      return { ...c, [active.providerId]: { ...cur, selectedModel: active.modelId } };
    });
  }, [active?.providerId, active?.modelId]);
  const [showKey, setShowKey] = useState(false);
  const [regionFilter, setRegionFilter] = useState<Region | "all">("all");
  const provider = PROVIDERS.find((p) => p.id === selectedId) ?? PROVIDERS[0];
  const cfg = configs[selectedId] ?? configs[PROVIDERS[0].id]!;
  const patch = (id: string, p: Partial<Cfg>) => {
    setSaveError(""); // 用户再编辑即清除上次保存失败横幅
    setConfigs((c) => ({ ...c, [id]: { ...c[id], ...p } }));
  };

  const activeProvider = active ? PROVIDERS.find((p) => p.id === active.providerId) : null;
  const activeModel = activeProvider?.models.find((m) => m.id === active?.modelId)
    ?? configs[active?.providerId ?? ""]?.models.find((m) => m.id === active?.modelId);

  async function testConn() {
    // v1.6.0（审计 C3）：真连通测试 —— 此前是 sleep(1100) + 长度≥8 判成功、
    // 延迟用 Math.random() 伪造的假测试，乱码 key 也显示「连接成功 · 347ms」。
    // 现在走后端 /api/models 真拉厂商模型列表，延迟为实测。
    // v1.6.4：自定义/中转站无预置 URL —— 空地址直接前置拦截（否则后端拼出 "/models" 相对地址必失败）。
    if (!cfg.baseUrl.trim()) { patch(selectedId, { status: "error", message: "请先填入 Base URL" }); return; }
    if (!cfg.apiKey.trim()) { patch(selectedId, { status: "error", message: "请先填入 API Key" }); return; }
    patch(selectedId, { status: "testing", message: "" });
    const t0 = performance.now();
    try {
      const r = await api.models(cfg.baseUrl, cfg.apiKey, selectedId);
      const err = r.provenance?.error as string | undefined;
      const ok = !err && (r.models?.length ?? 0) > 0;
      patch(selectedId, ok
        ? { status: "connected", latency: Math.round(performance.now() - t0), message: `连接成功（拉到 ${r.models!.length} 个模型）` }
        : { status: "error", message: `认证或探测失败：${err || "未返回任何模型"}` });
    } catch (e) {
      patch(selectedId, { status: "error", message: `连接失败：${e instanceof Error ? e.message : String(e)}` });
    }
  }

  async function fetchModels() {
    if (!cfg.baseUrl.trim()) { patch(selectedId, { status: "error", message: "请先填入 Base URL" }); return; }
    if (!cfg.apiKey.trim()) { patch(selectedId, { status: "error", message: "请先填入 API Key" }); return; }
    patch(selectedId, { fetching: true });
    try {
      const r = await api.models(cfg.baseUrl, cfg.apiKey, selectedId);
      const err = r.provenance?.error as string | undefined;
      const live = (r.models || []).map((m) => ({ id: m.id, label: m.label || m.id, context: "" }));
      const models = live.length ? live : (cfg.models.length ? cfg.models : provider.models);
      if (err && !live.length) {
        patch(selectedId, {
          fetching: false,
          models,
          selectedModel: cfg.selectedModel && models.some((m) => m.id === cfg.selectedModel)
            ? cfg.selectedModel
            : models[0]?.id || "",
          status: "error",
          message: models.length
            ? `拉取失败（${err}），当前展示已保存/内置清单`
            : `拉取失败：${err}（可在下方手动添加模型 ID）`,
        });
        return;
      }
      patch(selectedId, {
        fetching: false,
        models,
        // v1.6.4：实拉会整体替换清单 —— 旧 selectedModel（如手动加的模型）不在新清单时
        // 回退首选，否则「设为当前推理模型」按钮踩 find() undefined
        selectedModel: cfg.selectedModel && models.some((m) => m.id === cfg.selectedModel)
          ? cfg.selectedModel
          : models[0]?.id || "",
        status: "connected",
        // v1.6.4：自定义端点无内置清单可兜底 —— 提示改为引导手动添加
        message: live.length ? `已拉取 ${live.length} 个模型` : provider.models.length ? "使用内置模型清单" : "该端点未返回模型，可在下方手动添加",
      });
    } catch (e) {
      // 厂商 /models 拉取异常 → 回退静态清单并如实提示错误
      const models = cfg.models.length ? cfg.models : provider.models;
      const reason = e instanceof Error ? e.message : String(e);
      patch(selectedId, {
        fetching: false,
        models,
        selectedModel: cfg.selectedModel && models.some((m) => m.id === cfg.selectedModel)
          ? cfg.selectedModel
          : models[0]?.id || "",
        status: "error",
        message: models.length
          ? `拉取失败（${reason}），当前展示已保存/内置清单`
          : `拉取失败（${reason}），可在下方手动添加模型`,
      });
    }
  }

  // v1.6.4：手动模型管理（中转站主路径 —— 模型名千奇百怪，/v1/models 也不一定开）
  const [newModelId, setNewModelId] = useState("");
  function addManualModel() {
    const id = newModelId.trim();
    if (!id) return;
    if (cfg.models.some((m) => m.id === id)) {
      // 已存在：不重复添加，只补选中（可能是实拉过但没点选的）
      patch(selectedId, { selectedModel: cfg.selectedModel || id });
      setNewModelId("");
      return;
    }
    const models = [...cfg.models, { id, label: id, context: "", tag: "手动" }];
    // 首个手动添加的模型直接选中（「设为当前推理模型」按钮依赖 selectedModel 非空）
    patch(selectedId, { models, selectedModel: cfg.selectedModel || id });
    setNewModelId("");
  }
  function removeModel(id: string) {
    patch(selectedId, {
      models: cfg.models.filter((m) => m.id !== id),
      selectedModel: cfg.selectedModel === id ? "" : cfg.selectedModel,
    });
  }

  const list = useMemo(() => PROVIDERS.filter((p) => regionFilter === "all" || p.region === regionFilter), [regionFilter]);

  const statusDot = (s: Status) => s === "connected" ? "var(--down)" : s === "error" ? "var(--up)" : s === "testing" ? "var(--gold)" : "var(--muted-foreground)";

  return (
    <div className="h-full grid grid-cols-1 md:grid-cols-[210px_minmax(0,1fr)] gap-3 p-4 overflow-auto">
      {saveError && (
        <div className="col-span-full rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-start gap-2">
          <AlertCircle className="size-3.5 shrink-0 mt-0.5" />
          <span>{saveError}</span>
        </div>
      )}
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
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">{provider.region === "custom" ? "自定义" : provider.region === "domestic" ? "国内" : "国外"} · OpenAI 兼容</span>
                </div>
                <p className="text-xs text-muted-foreground mt-0.5">{provider.docsHint}</p>
              </div>
            </div>

            {/* Base URL */}
            <div className="mt-6">
              <label className="text-xs text-muted-foreground flex items-center gap-1.5 mb-1.5">
                <Link2 className="size-3.5" />
                {provider.baseUrl ? "Base URL（已预置，可修改）" : "Base URL（必填 · 中转站/自建网关端点，一般以 /v1 结尾）"}
              </label>
              <input
                value={cfg.baseUrl}
                onChange={(e) => {
                  const next = e.target.value;
                  const endpoint = (value: string) => value.trim().replace(/\/+$/, "");
                  patch(selectedId, { baseUrl: next, ...(endpoint(next) !== endpoint(cfg.baseUrl) ?
                    { apiKey: "", fallbackModelsText: "", status: "idle", message: "" } : {}) });
                }}
                placeholder={provider.baseUrl ? "" : "https://your-relay.example.com/v1"}
                className="w-full rounded-lg bg-input border border-border px-3 py-2.5 text-sm font-mono outline-none focus:border-gold/40 transition-colors placeholder:text-muted-foreground/70"
                spellCheck={false}
              />
            </div>

            {/* 思考深度（v1.6.5）：中转站在服务端配了不同思考深度时，这里选档位下发 */}
            <div className="mt-4">
              <label className="text-xs text-muted-foreground flex items-center gap-1.5 mb-1.5">
                <Gauge className="size-3.5" /> 思考深度（推理模型；默认不发参数）
              </label>
              <div className="flex gap-1.5">
                {([["", "默认"], ["low", "低"], ["medium", "中"], ["high", "高"]] as const).map(([v, label]) => {
                  const chosen = (cfg.reasoningEffort || "") === v;
                  return (
                    <button
                      key={v || "default"}
                      type="button"
                      onClick={() => patch(selectedId, { reasoningEffort: v })}
                      className={`flex-1 rounded-lg border py-1.5 text-xs transition-all ${chosen ? "border-gold bg-accent text-gold" : "border-border bg-secondary/30 text-foreground/85 hover:border-border/70"}`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
              <p className="text-[10.5px] text-muted-foreground/70 mt-1.5">
                低 = 回复更快，高 = 复杂问题更准。以 reasoning_effort 参数下发（端点需支持/透传）；
                选了档位或模型名含 thinking 等推理标识时，本软件会同步放宽超时与 token 上限，避免思考被误判超时。
              </p>
            </div>

            {/* API Key */}
            <div className="mt-4">
              <label className="text-xs text-muted-foreground flex items-center gap-1.5 mb-1.5"><KeyRound className="size-3.5" /> API Key</label>
              <div className="flex items-center rounded-lg bg-input border border-border focus-within:border-gold/40 transition-colors">
                <input
                  type={isMaskedApiKey(cfg.apiKey) || showKey ? "text" : "password"}
                  value={cfg.apiKey}
                  onFocus={(e) => { if (isMaskedApiKey(cfg.apiKey)) e.currentTarget.select(); }}
                  onChange={(e) => patch(selectedId, { apiKey: normalizeApiKeyInput(cfg.apiKey, e.target.value), status: "idle", message: "" })}
                  placeholder="sk-··· 在此填入你的密钥"
                  className="flex-1 bg-transparent px-3 py-2.5 text-sm font-mono outline-none placeholder:text-muted-foreground placeholder:font-sans"
                  spellCheck={false}
                />
                {isMaskedApiKey(cfg.apiKey) && (
                  <button
                    type="button"
                    title="清空当前密钥并重新输入"
                    onClick={() => patch(selectedId, { apiKey: "", status: "idle", message: "" })}
                    className="px-2 py-1 mr-1 rounded text-[11px] bg-secondary/70 text-muted-foreground hover:text-foreground transition-colors shrink-0"
                  >
                    更换
                  </button>
                )}
                <button
                  type="button"
                  disabled={isMaskedApiKey(cfg.apiKey)}
                  title={isMaskedApiKey(cfg.apiKey) ? "已加密保存在本机（仅展示末 4 位），点击「更换」或粘贴新密钥即可替换" : (showKey ? "隐藏密钥" : "显示密钥")}
                  onClick={() => setShowKey((s) => !s)}
                  className="px-3 text-muted-foreground hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {showKey && !isMaskedApiKey(cfg.apiKey) ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
              <p className="mt-1.5 text-xs text-muted-foreground">密钥加密保存在景渊的数据库中，调用模型时用于向配置的端点认证。</p>
            </div>

            {/* 测试连通性 */}
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
                  className="flex items-center gap-1.5 rounded-lg bg-gold px-3 py-1.5 text-xs text-gold-foreground hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                >
                  {cfg.fetching ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                  拉取最新模型
                </button>
              </div>

              {cfg.models.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border py-8 text-center text-xs text-muted-foreground">
                  {cfg.apiKey.trim() ? "点击「拉取最新模型」获取列表，或在下方手动添加模型" : "请先填入 API Key"}
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
                        className={`group relative text-left p-3 rounded-lg border transition-all ${chosen ? "border-gold bg-accent" : "border-border hover:border-border/70 bg-secondary/30"}`}
                      >
                        <div className="flex items-center justify-between">
                          <span className={`text-sm ${chosen ? "text-gold" : "text-foreground/90"}`}>{m.label}</span>
                          {chosen && <Check className="size-4 text-gold" />}
                          {/* v1.6.4：悬停移除（span 而非 button —— 不在 button 里嵌 button） */}
                          {!chosen && (
                            <span
                              title="移除该模型"
                              onClick={(e) => { e.stopPropagation(); removeModel(m.id); }}
                              className="grid place-items-center size-6 -my-1 -mr-1 rounded text-muted-foreground/60 hover:text-up hover:bg-secondary opacity-0 group-hover:opacity-100 transition-all"
                            >
                              <X className="size-3.5" />
                            </span>
                          )}
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

              {/* v1.6.4：手动添加模型 —— 中转站主路径（模型名不规律、/v1/models 未必开放）；
                  预设厂商同样可用（新模型未进静态清单、或 URL 已改指向中转时） */}
              <label className="grid gap-1 pt-2 text-xs text-muted-foreground">
                同一供应商的备用模型（按顺序，最多四个，以逗号分隔）
                <input className="rounded border bg-input px-2 py-1.5 font-mono text-foreground" value={cfg.fallbackModelsText}
                  onChange={(event) => patch(selectedId, { fallbackModelsText: event.target.value })} />
              </label>
              <div className="mt-3 flex items-center gap-2">
                <input
                  value={newModelId}
                  onChange={(e) => setNewModelId(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !isImeKeyEvent(e.nativeEvent)) addManualModel(); }}
                  placeholder="手动添加模型 ID，如 gpt-4o / claude-sonnet-5（按中转站的模型名填）"
                  className="flex-1 rounded-lg bg-input border border-border px-3 py-2 text-xs font-mono outline-none focus:border-gold/40 transition-colors"
                  spellCheck={false}
                />
                <button
                  onClick={addManualModel}
                  disabled={!newModelId.trim()}
                  className="flex items-center gap-1 rounded-lg bg-secondary hover:bg-secondary/70 px-3 py-2 text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Plus className="size-3.5" /> 添加
                </button>
              </div>

              <AnimatePresence>
                {cfg.selectedModel && (
                  <motion.button
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0 }}
                    // v1.6.4：漏填 Base URL 时禁用 —— 空地址经后端 _resolve 会拼成
                    // 「官方 DeepSeek URL + 中转 key」的必 401 组合，不如在源头拦下
                    disabled={!cfg.baseUrl.trim()}
                    title={cfg.baseUrl.trim() ? undefined : "请先填入 Base URL"}
                    onClick={() => {
                      // v1.6.4：去 ! 断言 —— 清单被「拉取最新模型」整体替换等场景下
                      // selectedModel 可能已不在清单，find 回 undefined 直接崩整个应用
                      const m = cfg.models.find((x) => x.id === cfg.selectedModel);
                      if (!m) {
                        patch(selectedId, { selectedModel: cfg.models[0]?.id || "" });
                        return;
                      }
                      onActivate({ providerId: provider.id, modelId: m.id, provider: provider.name, model: m.id, accent: provider.accent });
                    }}
                    className="mt-4 w-full flex items-center justify-center gap-2 rounded-lg border border-gold/40 bg-accent/60 py-2.5 text-sm text-gold hover:bg-accent transition-colors overflow-hidden disabled:opacity-40 disabled:cursor-not-allowed"
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
