import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Cpu, Settings2 } from "lucide-react";
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

export function loadPickerOptions(): PickerOption[] {
  const saved = readSaved();
  const opts: PickerOption[] = [];
  for (const p of PROVIDERS) {
    const s = saved[p.id] || {};
    if (!(s.apiKey || "").trim()) continue;
    const rawModels: string[] = Array.isArray(s.models) && s.models.length
      ? s.models.map((m: any) => (typeof m === "string" ? m : m?.id || "")).filter(Boolean)
      : p.models.map((m) => m.id);
    // 去重 + 保证 selectedModel 在列（旧数据 selectedModel 可能不在 models 里）
    const ids: string[] = [];
    for (const id of [...rawModels, s.selectedModel || ""].filter(Boolean)) {
      if (!ids.includes(id)) ids.push(id);
    }
    for (const model of ids) {
      opts.push({
        providerId: p.id,
        provider: p.name,
        model,
        accent: p.accent,
        llm: {
          base_url: (s.baseUrl || p.baseUrl || "").trim(),
          api_key: (s.apiKey || "").trim(),
          model,
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

/** 选中即持久化（localStorage + 后端 active-model）。 */
function persistActive(o: PickerOption) {
  const payload = {
    providerId: o.providerId, modelId: o.model,
    provider: o.provider, model: o.model, accent: o.accent,
  };
  try {
    localStorage.setItem("finresearch_active", JSON.stringify(payload));
  } catch { /* 存不进就只走后端 */ }
  api.saveLocalSetting("active-model", payload).catch(() => {});
}

export function ModelPicker({
  active,
  onChange,
  onGoSettings,
}: {
  active?: { provider: string; model: string } | null;
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

  // 默认选中：active-model 精确匹配 > prop 匹配 > 第一项
  const defaultIdx = useMemo(() => {
    let a: any = null;
    try { a = JSON.parse(localStorage.getItem("finresearch_active") || "null"); } catch { /* ignore */ }
    if (a?.providerId && a?.modelId) {
      const i = options.findIndex((o) => o.providerId === a.providerId && o.model === a.modelId);
      if (i >= 0) return i;
    }
    if (active) {
      const i = options.findIndex((o) => o.provider === active.provider && o.model === active.model);
      if (i >= 0) return i;
    }
    return 0;
  }, [options, active]);
  const [idx, setIdx] = useState<number>(defaultIdx);
  const [open, setOpen] = useState(false);
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
        <ChevronDown className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-[calc(100%+6px)] z-50 max-h-80 w-[280px] overflow-y-auto rounded-lg border border-border bg-card p-1.5 shadow-xl shadow-black/30">
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
