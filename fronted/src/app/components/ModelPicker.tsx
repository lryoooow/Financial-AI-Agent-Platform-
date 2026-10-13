import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Cpu } from "lucide-react";
import { PROVIDERS } from "../data/providers";
import { api, type LlmConfig, type ReasoningLevel } from "../api/client";

const REASONING_KEY = "finresearch_reasoning_effort";
const REASONING_LEVELS: ReasoningLevel[] = ["low", "medium", "high", "max"];

function loadReasoningLevel(): ReasoningLevel {
  const saved = localStorage.getItem(REASONING_KEY);
  return REASONING_LEVELS.includes(saved as ReasoningLevel) ? saved as ReasoningLevel : "low";
}

/**
 * 行内模型选择器（根因修复 #4/#5）。
 *
 * 读取 localStorage `finresearch_keys`（Settings 页保存的 per-provider {baseUrl,apiKey,selectedModel}），
 * 列出所有"已填 key 且选了模型"的供应商模型；用户在哪生成研究就在哪选模型，不再被"未设当前模型"硬拦截。
 * 选中即向上抛出 {base_url, api_key, model}，由 Workspace 随研究请求传后端真正使用。
 *
 * 注意：activeModel prop 只有 {provider,model,accent}（无 baseUrl/apiKey），所以密钥从这里读 localStorage，
 * 而不是从 prop 传——避免密钥在组件树里传递。
 */
export interface PickerOption {
  providerId: string;
  provider: string;
  model: string;
  accent: string;
  llm: LlmConfig;
}

export function loadPickerOptions(): PickerOption[] {
  let saved: Record<string, any> = {};
  try {
    saved = JSON.parse(localStorage.getItem("finresearch_keys") || "{}");
  } catch {
    saved = {};
  }
  const opts: PickerOption[] = [];
  for (const p of PROVIDERS) {
    const s = saved[p.id];
    if (!s || !(s.apiKey || "").trim() || !(s.selectedModel || "").trim()) continue;
    opts.push({
      providerId: p.id,
      provider: p.name,
      model: s.selectedModel,
      accent: p.accent,
      llm: {
        base_url: (s.baseUrl || p.baseUrl || "").trim(),
        api_key: (s.apiKey || "").trim(),
        model: (s.selectedModel || "").trim(),
        reasoning_effort: loadReasoningLevel(),
      },
    });
  }
  return opts;
}

/** 取当前"已设为推理模型"的 LlmConfig；未设或未配置则回退第一个已配置项；都没有返回 null。
 *  供 chart AI 速读等不便层层透传的场景直接读用户选择。 */
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
  // 默认选中：与 active {provider,model} 匹配的项，否则第一项
  const defaultIdx = useMemo(() => {
    if (!active) return 0;
    const i = options.findIndex((o) => o.provider === active.provider && o.model === active.model);
    return i >= 0 ? i : 0;
  }, [options, active]);
  const [idx, setIdx] = useState<number>(defaultIdx);
  const [open, setOpen] = useState(false);
  const [reasoning, setReasoning] = useState<ReasoningLevel>(() => loadReasoningLevel());
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setIdx(defaultIdx);
  }, [defaultIdx]);

  useEffect(() => {
    if (options.length) onChange({ ...options[Math.min(idx, options.length - 1)].llm, reasoning_effort: reasoning });
    else onChange(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.length, idx, reasoning]);

  useEffect(() => {
    localStorage.setItem(REASONING_KEY, reasoning);
  }, [reasoning]);

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

  const cur = options[Math.min(idx, options.length - 1)];
  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-[220px] items-center gap-1.5 rounded-md bg-secondary/60 px-2 py-1 text-[11px] transition-colors hover:bg-secondary"
      >
        <Cpu className="size-3 shrink-0" style={{ color: cur.accent }} />
        <span className="hidden shrink-0 text-muted-foreground sm:inline">模型</span>
        <span className="truncate text-foreground/85">{cur.provider} · {cur.model}</span>
        <span className="rounded bg-background/60 px-1 text-[9px] uppercase text-gold">{reasoning}</span>
        <ChevronDown className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-[calc(100%+6px)] z-50 max-h-72 w-[260px] overflow-y-auto rounded-lg border border-border bg-card p-1.5 shadow-xl shadow-black/30">
          {options.map((o, i) => {
            const selected = i === idx;
            return (
              <button
                key={`${o.providerId}-${o.model}`}
                type="button"
                onClick={() => {
                  setIdx(i);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-3 rounded-md px-2.5 py-2 text-left transition-colors ${selected ? "bg-accent text-gold" : "text-foreground/85 hover:bg-secondary"}`}
              >
                <span className="min-w-0">
                  <span className="block truncate text-xs">{o.provider}</span>
                  <span className="block truncate text-[10px] text-muted-foreground">{o.model}</span>
                </span>
                {selected && <Check className="size-3.5 shrink-0 text-gold" />}
              </button>
            );
          })}
          <div className="mt-1 border-t border-border px-1.5 pt-2 pb-1">
            <div className="mb-1.5 flex items-center justify-between text-[10px] text-muted-foreground"><span>思考强度</span><span>默认 low</span></div>
            <div className="grid grid-cols-4 gap-1">
              {REASONING_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() => setReasoning(level)}
                  className={`rounded px-1 py-1 text-[9px] uppercase ${reasoning === level ? "bg-gold text-[#17140a]" : "bg-secondary text-muted-foreground hover:text-foreground"}`}
                >
                  {level}
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-[9px] leading-relaxed text-muted-foreground/70">档位为定性推理强度；后端会按模型能力映射，不等同于精确 token 上限。</p>
          </div>
        </div>
      )}
    </div>
  );
}
