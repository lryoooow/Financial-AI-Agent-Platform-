import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { loadActiveLlm } from "../ModelPicker";

interface Props {
  code?: string;
  kind: "intraday" | "kline";
  point?: { date?: string; time?: string; price?: number; close?: number };
  prevClose: number;
}

export function ChartRead({ code, kind, point, prevClose }: Props) {
  const [result, setResult] = useState("");
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  const pointKey = point?.date || point?.time || "";
  useEffect(() => {
    request.current?.abort();
    request.current = null;
    setLoading(false);
    setResult("");
  }, [code, kind, pointKey]);
  useEffect(() => () => request.current?.abort(), []);

  async function read() {
    if (!code || !point || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const label = point.date || point.time || "";
    setLoading(true);
    setResult("");
    try {
      const response = await api.chartAnalyze({
        code, kind, point: { ...point, price: point.price ?? point.close }, prevClose,
        llm: loadActiveLlm() ?? undefined,
      }, controller.signal);
      if (!controller.signal.aborted) setResult([label, response.text?.trim() || "当前没有可用解读"].filter(Boolean).join(" · "));
    } catch {
      if (!controller.signal.aborted) setResult("速读暂不可用，请稍后重试。");
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  return <div className="flex min-w-0 items-start gap-2 px-2 pb-1 text-xs">
    <button type="button" onClick={() => void read()} disabled={loading || !code || !point}
      className="shrink-0 rounded border px-2 py-0.5 text-gold hover:bg-accent disabled:opacity-60">
      {loading ? "解读中…" : "AI 速读"}
    </button>
    <span aria-live="polite" className="min-w-0 max-h-14 overflow-auto text-muted-foreground">
      {result || (point ? point.date || point.time : "")}
    </span>
  </div>;
}
