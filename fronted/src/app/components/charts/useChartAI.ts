import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { loadActiveLlm } from "../ModelPicker";

const CHART_AI_CACHE_TTL_MS = 30_000;
const chartAiCache = new Map<string, { text: string; expiresAt: number }>();

/**
 * 走势线轻量 AI 速读（req5）。
 * 悬停某点稳定 450ms 后，调后端 POST /api/chart/analyze（单次便宜 LLM），
 * 把 1-2 句速读塞进图表 Tooltip。按点签名缓存，避免每像素重复请求；
 * 后端没起 / 无 LLM 时静默（不显示 AI 行），不影响看图。
 */
export function useChartAI(
  code: string | undefined,
  kind: "intraday" | "kline",
  point: Record<string, any> | null,
  prevClose: number
) {
  const [ai, setAi] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const lastSig = useRef<string>("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    request.current?.abort();
    request.current = null;
    // v1.6.3（C5）：切点先清旧速读文案（悬停跨点残留旧文案），有缓存则立即回填
    setAi(undefined);
    if (!code || !point) {
      lastSig.current = "";
      return;
    }
    // 归一化 price（分时用 price，K线用 close）
    const price = point.price ?? point.close;
    const activeLlm = loadActiveLlm() ?? undefined;
    const sig = `${code}|${kind}|${point.time ?? point.date ?? ""}|${price ?? ""}|${activeLlm?.base_url ?? ""}|${activeLlm?.model ?? ""}`;
    const cached = chartAiCache.get(sig);
    if (cached && cached.expiresAt > Date.now()) {
      lastSig.current = sig;
      setAi(cached.text);
      setLoading(false);
      return;
    }
    timer.current = setTimeout(async () => {
      const controller = new AbortController();
      request.current = controller;
      setLoading(true);
      try {
        const r = await api.chartAnalyze({
          code,
          kind,
          point: { ...point, price },
          prevClose,
          llm: activeLlm,  // URL / Key / model 作为同一供应商配置原子传递
        }, controller.signal);
        const text = (r.text || "").trim();
        if (!text || text.startsWith("(") || text.startsWith("（未配置")) return;
        lastSig.current = sig;
        chartAiCache.set(sig, { text, expiresAt: Date.now() + CHART_AI_CACHE_TTL_MS });
        setAi(text);
      } catch (error) {
        if ((error as DOMException)?.name === "AbortError") return;
        // 静默：后端不可用时不显示 AI 行
      } finally {
        if (request.current === controller) {
          request.current = null;
          setLoading(false);
        }
      }
    }, 650);
    return () => {
      if (timer.current) clearTimeout(timer.current);
      request.current?.abort();
      request.current = null;
    };
  }, [code, kind, point, prevClose]);

  return { ai, loading };
}
