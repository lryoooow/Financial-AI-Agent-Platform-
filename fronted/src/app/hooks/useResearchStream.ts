import { useCallback, useEffect, useRef } from "react";
import { api } from "../api/client";

/**
 * 研究 SSE 连接的生命周期管理（v1.6.0 审计 C2 / v1.6.3 D1）。
 *
 * 此前 Workspace 直接 new EventSource 且只在 onmessage/onerror 里关闭：
 * - 组件重挂载后旧连接永不关闭（连接泄漏 + 旧闭包对已卸载组件 setState）；
 * - 运行中的研究从 UI 消失且无法找回。
 *
 * v1.6.3（D1）：断线不再永久判死 —— 有限次自动重连（3 次指数退避 1s/2s/4s），
 * 重连由后端 history 回放恢复进度（后端本就支持断连续跑+回放）；重连耗尽才上报
 * __disconnected__（调用方仍可切页/刷新触发挂载重连兜底）。
 */
const MAX_RETRIES = 3;

export function useResearchStream() {
  const esRef = useRef<EventSource | null>(null);
  const retryRef = useRef(0);
  const retryTimerRef = useRef<number | null>(null);
  // 最近一次 connect 的参数 + 回调（重连时复用）
  const lastRef = useRef<{ taskId: string; code: string; template: string; onEvent: (ev: any) => void } | null>(null);

  const close = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    try { esRef.current?.close(); } catch { /* already closed */ }
    esRef.current = null;
    retryRef.current = 0;
    lastRef.current = null;
  }, []);

  const connect = useCallback(
    (taskId: string, code: string, template: string, onEvent: (ev: any) => void) => {
      close();
      retryRef.current = 0;
      lastRef.current = { taskId, code, template, onEvent };

      const open = () => {
        const last = lastRef.current;
        if (!last) return;
        const es = new EventSource(api.researchStreamUrl(last.taskId, last.code, last.template));
        es.onmessage = (e: MessageEvent) => {
          retryRef.current = 0; // 收到数据即视为连接健康，重试计数清零
          try {
            last.onEvent(JSON.parse(e.data));
          } catch { /* 非 JSON 帧忽略（后端约定每帧一个 JSON 事件） */ }
        };
        es.onerror = () => {
          const wasOurs = esRef.current === es;
          try { es.close(); } catch { /* noop */ }
          if (esRef.current === es) esRef.current = null;
          if (!wasOurs || !lastRef.current) return;
          // v1.6.3：有限次自动重连（后端 history 回放恢复进度），耗尽才判死
          if (retryRef.current < MAX_RETRIES) {
            retryRef.current += 1;
            const delay = 1000 * 2 ** (retryRef.current - 1);
            retryTimerRef.current = window.setTimeout(() => {
              retryTimerRef.current = null;
              if (lastRef.current) open();
            }, delay);
          } else {
            lastRef.current.onEvent({ type: "__disconnected__" });
          }
        };
        esRef.current = es;
      };

      open();
    },
    [close],
  );

  useEffect(() => close, [close]);

  return { connect, close, isConnected: () => esRef.current !== null };
}
