import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Cpu, Gauge, Settings2 } from "lucide-react";
import { PROVIDERS } from "../data/providers";
import { api, type LlmConfig } from "../api/client";

/**
 * 行内模型选择器（v1.6.1：纯模型列表）。
 *
 * 用户反馈明确要求：不再展示模型厂商，直接列具体模型可选。
 * —— 每项 = 一个具体模型（厂商仅以左侧色点隐式区分）；
 * —— 选中即持久化 active-model（localStorage + 后端），与后端 _active_llm_cfg 口径闭环；
 * —— options 刷新（Settings 换 key/拉新模型）后自动用新配置重发当前选择
 *    （修「换 key 后会话仍带旧 key 请求」的旧链）。
 *
 * 密钥仍从 localStorage `finresearch_keys` 读（activeModel prop 无密钥）。
 */
export interface PickerOption {
  providerId: string;
  provider: string;
  model: string;
  accent: string;
  llm: LlmConfig;
}

function readSaved(): Record<string, any> {
  try {
    return JSON.parse(localStorage.getItem("finresearch_keys") || "{}");
  } catch {
    return {};
  }
}

// v1.7.0：思考深度档位快捷切换（低 = 回复更快的直控开关）。
const EFFORT_LEVELS: [string, string][] = [["", "默认"], ["low", "低"], ["medium", "中"], ["high", "高"]];
const EFFORT_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高" };

/** 读-改-写【整份】finresearch_keys 的该 provider 记录（保留 baseUrl/apiKey/models/
 *  selectedModel 等既有字段），写 localStorage + 广播 + PUT 后端。
 *  广播即刷新：本实例经 configs-updated → loadPickerOptions → options 新引用 →
 *  既有 effect 自动用带新 reasoning_effort 的 llm 重发 onChange（下一条消息生效）。 */
function persistReasoningEffort(providerId: string, effort: string): Promise<unknown> {
  const saved = readSaved();
  const rec = saved[providerId] || {};
  saved[providerId] = { ...rec, reasoningEffort: effort };
  try {
    localStorage.setItem("finresearch_keys", JSON.stringify(saved));
  } catch { /* 存不进就只走后端 */ }
  window.dispatchEvent(new CustomEvent("finresearch:configs-updated"));
  return api.saveLocalSetting("model-configs", saved);
}

export function loadPickerOptions(): PickerOption[] {
  const saved = readSaved();
  const opts: PickerOption[] = [];
  for (const p of PROVIDERS) {
    const s = saved[p.id] || {};
    const baseUrl = (s.baseUrl || p.baseUrl || "").trim();
    // baseUrl 必须非空（自定义/中转站无预置值）：空 base_url 一旦放行，
    // 后端 llm._resolve 会回退到官方 DeepSeek 端点，拼成「官方 URL + 中转 key」的必 401 组合。
    if (!(s.apiKey || "").trim() || !baseUrl) continue;
    // v1.6.4：只认「数组」不认「非空」—— 用户删空清单（含预设厂商删光静态模型）必须被尊重，
    // 否则保存的空数组被 p.models 静态清单"复活"，删除形同虚设。无数组 = 从未保存过，才回退预置。
    const rawModels: string[] = Array.isArray(s.models)
      ? s.models.map((m: any) => (typeof m === "string" ? m : m?.id || "")).filter(Boolean)
      : p.models.map((m) => m.id);
    // 去重 + selectedModel 排首位（v1.6.3 E：无 active-model 时默认选中用户在 Settings
    // 点选的 selectedModel，而非厂商列表的第一个——旧实现按字母序抓到任意微调模型）
    const ids: string[] = [];
    for (const id of [s.selectedModel || "", ...rawModels].filter(Boolean)) {
      if (!ids.includes(id)) ids.push(id);
    }
    for (const model of ids) {
      opts.push({
        providerId: p.id,
        provider: p.name,
        model,
        accent: p.accent,
        llm: {
          provider_id: p.id,
          base_url: baseUrl,
          api_key: (s.apiKey || "").trim(),
          model,
          // v1.6.5：思考深度档位（用户显式选择时才携带；后端 build_llm 透传 reasoning_effort）
          ...((s.reasoningEffort || "").trim() ? { reasoning_effort: (s.reasoningEffort || "").trim() } : {}),
        },
      });
    }
  }
  return opts;
}

/** 取当前激活 LlmConfig（供图表速读等不便层层透传的场景）；
 *  active-model 的 modelId 优先，失配回退第一个已配置项。 */
export function loadActiveLlm(): LlmConfig | null {
  const options = loadPickerOptions();
  if (!options.length) return null;
  let active: any = null;
  try {
    active = JSON.parse(localStorage.getItem("finresearch_active") || "null");
  } catch {
    active = null;
  }
  if (active && active.providerId && active.modelId) {
    const m = options.find((o) => o.providerId === active.providerId && o.model === active.modelId);
    if (m) return m.llm;
  }
  return options[0].llm;
}

/** 选中即持久化（localStorage + 后端 active-model）。v1.6.3：广播 model-changed
 *  —— 其余常驻 ModelPicker 实例（聊天页/研究页）据此同步默认选中，App 据此置
 *  userTouchedRef（防迟到 GET 覆盖用户选择）。 */
function persistActive(o: PickerOption) {
  const payload = {
    providerId: o.providerId, modelId: o.model,
    provider: o.provider, model: o.model, accent: o.accent,
  };
  try {
    localStorage.setItem("finresearch_active", JSON.stringify(payload));
  } catch { /* 存不进就只走后端 */ }
  api.saveLocalSetting("active-model", payload).catch(() => {});
  window.dispatchEvent(new CustomEvent("finresearch:model-changed", { detail: payload }));
}

export function ModelPicker({
  active,
  onChange,
  onGoSettings,
}: {
  active?: { provider: string; model: string; modelId?: string } | null;
  onChange: (llm: LlmConfig | null) => void;
  onGoSettings: () => void;
}) {
  const [options, setOptions] = useState<PickerOption[]>(() => loadPickerOptions());
  useEffect(() => {
    let cancelled = false;
    api.localSetting("model-configs")
      .then((r) => {
        if (cancelled || !r.payload) return;
        localStorage.setItem("finresearch_keys", JSON.stringify(r.payload));
        setOptions(loadPickerOptions());
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  // v1.6.2：Settings 保存（含换 key）即时同步 —— 广播一响就重读 localStorage，
  // 下面的重发 effect 会用新配置刷新宿主组件的 llm（杀"带着旧 key 请求"的缓存链）
  // v1.6.3（C3/C4）：+ model-changed（另一实例换了模型 → 本实例刷新默认选中）
  useEffect(() => {
    const refresh = () => setOptions(loadPickerOptions());
    window.addEventListener("finresearch:configs-updated", refresh);
    window.addEventListener("finresearch:model-changed", refresh);
    return () => {
      window.removeEventListener("finresearch:configs-updated", refresh);
      window.removeEventListener("finresearch:model-changed", refresh);
    };
  }, []);

  // 默认选中：active-model 精确匹配 > prop 匹配 > 第一项
  const defaultIdx = useMemo(() => {
    let a: any = null;
    try { a = JSON.parse(localStorage.getItem("finresearch_active") || "null"); } catch { /* ignore */ }
    if (a?.providerId && a?.modelId) {
      const i = options.findIndex((o) => o.providerId === a.providerId && o.model === a.modelId);
      if (i >= 0) return i;
    }
    if (active) {
      const targetId = active.modelId || active.model;
      const i = options.findIndex((o) => o.provider === active.provider && o.model === targetId);
      if (i >= 0) return i;
    }
    return 0;
  }, [options, active]);
  const [idx, setIdx] = useState<number>(defaultIdx);
  const [open, setOpen] = useState(false);
  // v1.7.0：快捷切档的失败提示（下拉内一行红字；PUT 失败时本机已生效，重启后回落）
  const [effortHint, setEffortHint] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => { setIdx(defaultIdx); }, [defaultIdx]);

  // 依赖 options（引用）而非 length：后端 GET 刷新 localStorage 后 options 是新数组，
  // 用新配置重发当前选择 —— 修「换 key 后整个会话仍带旧 key」的隐藏链。
  useEffect(() => {
    if (options.length) onChange(options[Math.min(idx, options.length - 1)].llm);
    else onChange(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options, idx]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  if (!options.length) {
    return (
      <button
        type="button"
        onClick={onGoSettings}
        className="flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-up/40 bg-up/5 text-up hover:bg-up/10 transition-colors"
        title="尚未配置任何模型，点击去配置"
      >
        <Cpu className="size-3" />
        未配置模型 · 去配置
      </button>
    );
  }

  const safeIdx = Math.min(idx, options.length - 1);
  const cur = options[safeIdx];

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-[260px] items-center gap-1.5 rounded-md bg-secondary/60 px-2 py-1 text-[11px] transition-colors hover:bg-secondary"
        title={`${cur.provider} · ${cur.model}`}
      >
        <Cpu className="size-3 shrink-0" style={{ color: cur.accent }} />
        <span className="hidden shrink-0 text-muted-foreground sm:inline">模型</span>
        <span className="truncate font-mono text-foreground/85">{cur.model}</span>
        {cur.llm.reasoning_effort && (
          <span
            className="flex shrink-0 items-center gap-0.5 rounded bg-gold/15 px-1 py-0.5 text-[9px] text-gold"
            title="思考深度（点开下拉可切换；低 = 回复更快）"
          >
            <Gauge className="size-2.5" />
            {EFFORT_LABELS[cur.llm.reasoning_effort] || cur.llm.reasoning_effort}
          </span>
        )}
        <ChevronDown className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-[calc(100%+6px)] z-50 max-h-80 w-[280px] overflow-y-auto rounded-lg border border-border bg-card p-1.5 shadow-xl shadow-black/30">
          {/* v1.7.0：当前供应商的思考深度快捷切换（聊天页一秒切低档提速） */}
          <div className="mb-1 border-b border-border/60 px-1 pb-1.5 pt-1">
            <div className="flex items-center gap-1.5">
              <span className="shrink-0 text-[10px] text-muted-foreground">思考深度</span>
              <div className="flex flex-1 gap-1">
                {EFFORT_LEVELS.map(([v, label]) => {
                  const on = (cur.llm.reasoning_effort || "") === v;
                  return (
                    <button
                      key={v || "default"}
                      type="button"
                      onClick={() => {
                        setEffortHint("");
                        persistReasoningEffort(cur.providerId, v).catch((e) => {
                          setEffortHint(`档位保存失败（${e instanceof Error ? e.message : String(e)}）——本机已生效，重启后回落，可到设置页重试`);
                        });
                      }}
                      className={`flex-1 rounded px-1 py-1 text-[10px] transition-colors ${on ? "bg-accent text-gold" : "text-foreground/75 hover:bg-secondary"}`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
            {effortHint && <div className="mt-1 text-[10px] leading-snug text-destructive">{effortHint}</div>}
          </div>
          {options.map((o, i) => {
            const selected = i === safeIdx;
            return (
              <button
                key={`${o.providerId}-${o.model}`}
                type="button"
                onClick={() => {
                  setIdx(i);
                  setOpen(false);
                  persistActive(o);
                  onChange(o.llm);
                }}
                className={`flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left transition-colors ${selected ? "bg-accent text-gold" : "text-foreground/85 hover:bg-secondary"}`}
              >
                <span className="size-1.5 rounded-full shrink-0" style={{ background: o.accent }} />
                <span className="min-w-0 flex-1 truncate text-[11px] font-mono">{o.model}</span>
                {selected && <Check className="size-3.5 shrink-0 text-gold" />}
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => { setOpen(false); onGoSettings(); }}
            className="mt-1 flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[10px] text-muted-foreground hover:bg-secondary transition-colors"
          >
            <Settings2 className="size-3" />
            管理模型与密钥…
          </button>
        </div>
      )}
    </div>
  );
}
