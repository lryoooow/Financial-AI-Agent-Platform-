import { useEffect, useState, type ReactNode } from "react";
import { LockKeyhole } from "lucide-react";
import { api } from "../api/client";
import { isImeKeyEvent } from "../lib/ime";
import { Skeleton } from "./ui/skeleton";

export function WebAuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "ready" | "locked" | "unavailable">("checking");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function verify(initial = false) {
    try {
      const result = await api.webAuthStatus();
      setState(result.authenticated ? "ready" : "locked");
    } catch {
      if (initial) setState("unavailable");
    }
  }
  useEffect(() => {
    void verify(true);
    const check = () => void verify();
    window.addEventListener("focus", check);
    window.addEventListener("jingyuan:auth-required", check);
    return () => { window.removeEventListener("focus", check); window.removeEventListener("jingyuan:auth-required", check); };
  }, []);
  if (state === "ready") return children;

  async function unlock() {
    if (busy || !token.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api.webAuthLogin(token);
      setToken("");
      await verify(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败。");
    } finally {
      setBusy(false);
    }
  }

  return <main className="grid size-full place-content-center bg-background p-4">
    <section className="w-[min(100vw-32px,390px)] space-y-3 rounded-lg border bg-card p-5 shadow-[var(--shadow-card)]">
      <h1 className="flex items-center gap-2 text-lg font-semibold"><LockKeyhole className="size-5 text-gold" /> 景渊工作台</h1>
      {state === "checking" && <Skeleton className="h-16" />}
      {state === "unavailable" && <>
        <p role="alert" className="text-muted-foreground">服务暂未连接。</p>
        <button type="button" onClick={() => { setState("checking"); void verify(true); }} className="rounded border px-3 py-1 text-gold">重新连接</button>
      </>}
      {state === "locked" && <>
        <label className="grid gap-1">部署访问口令
          <input autoFocus type="password" autoComplete="current-password" value={token} maxLength={1024}
            className="rounded border bg-input px-2 py-1.5" onChange={(event) => setToken(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !isImeKeyEvent(event.nativeEvent)) void unlock(); }} />
        </label>
        {error && <p role="alert" className="text-destructive">{error}</p>}
        <button type="button" disabled={busy || !token.trim()} onClick={() => void unlock()}
          className="rounded bg-action px-3 py-1.5 text-gold-foreground disabled:opacity-50">{busy ? "验证中…" : "进入工作台"}</button>
      </>}
    </section>
  </main>;
}
