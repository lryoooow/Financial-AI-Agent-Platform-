import { useCallback, useEffect, useRef } from "react";
import { api } from "../api/client";

/**
 * 研究 SSE 连接的生命周期管理（v1.6.0，审计 C2）。
 *
 * 此前 Workspace 直接 new EventSource 且只在 onmessage/onerror 里关闭：
 * - 组件重挂载（切换「隐藏行情」触发父结构切换）后旧连接永不关闭 —— 连接泄漏 +
 *   旧闭包持续对已卸载组件 setState；
 * - 运行中的研究从 UI 消失且无法找回（后端明明支持 history 回放 + 断连续跑）。
 *
 * 本 hook 三件事：
 * 1. connect() 前先关旧连接（同一时刻至多一条研究流）；
 * 2. 组件卸载必关（useEffect cleanup）；
 * 3. onEvent 统一分发，连接层错误以 { type: "__disconnected__" } 上报，由调用方决定 UI。
 */
export function useResearchStream() {
  const esRef = useRef<EventSource | null>(null);

  const close = useCallback(() => {
    try { esRef.current?.close(); } catch { /* already closed */ }
    esRef.current = null;
  }, []);

  const connect = useCallback(
    (taskId: string, code: string, template: string, onEvent: (ev: any) => void) => {
      close();
      const es = new EventSource(api.researchStreamUrl(taskId, code, template));
      es.onmessage = (e: MessageEvent) => {
        try {
          onEvent(JSON.parse(e.data));
        } catch { /* 非 JSON 帧忽略（后端约定每帧一个 JSON 事件） */ }
      };
      es.onerror = () => {
        const wasOurs = esRef.current === es;
        try { es.close(); } catch { /* noop */ }
        if (esRef.current === es) esRef.current = null;
        if (wasOurs) onEvent({ type: "__disconnected__" });
      };
      esRef.current = es;
    },
    [close],
  );

  useEffect(() => close, [close]);

  return { connect, close, isConnected: () => esRef.current !== null };
}
