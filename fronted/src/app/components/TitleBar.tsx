import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Wifi, WifiOff, Minus, Square, Copy, X, Search, LogOut } from "lucide-react";
import { ThemeToggle } from "./ThemeToggle";
import { api } from "../api/client";
import logoMark from "../assets/logo-mark.svg";

const noDrag = { WebkitAppRegion: "no-drag" } as CSSProperties;

export function TitleBar({ onSearch, modelSelector }: { onSearch: () => void; modelSelector?: ReactNode }) {
  const [maximized, setMaximized] = useState(false);
  const [health, setHealth] = useState<boolean | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const win = window.finresearch?.window;
  const nativeMac = window.finresearch?.platform === "darwin";
  const isMacKey = nativeMac || (typeof navigator !== "undefined" && /Mac|iPhone|iPad/i.test(navigator.platform));
  useEffect(() => {
    let alive = true;
    const check = () => {
      if (document.hidden) return;
      api.health().then((value) => { if (alive) setHealth(value.status === "ok"); })
        .catch(() => { if (alive) setHealth(false); });
    };
    check();
    if (!win) {
      api.webAuthStatus().then((r) => { if (alive) setAuthRequired(!!r.required); }).catch(() => {});
    }
    const timer = setInterval(check, 20_000);
    return () => { alive = false; clearInterval(timer); };
  }, [win]);
  useEffect(() => {
    if (!win) return;
    win.isMaximized().then(setMaximized).catch(() => {});
    return win.onMaximizeChange(setMaximized);
  }, [win]);

  const iconButton = "grid h-9 w-10 place-items-center text-muted-foreground transition-colors hover:bg-accent hover:text-foreground";
  return (
    <header className="relative flex h-9 shrink-0 items-center justify-between gap-2 border-b bg-sidebar px-2"
      style={{ WebkitAppRegion: win ? "drag" : undefined } as CSSProperties}
      onDoubleClick={() => win?.maximizeToggle()}>
      <div className={"flex shrink-0 items-center gap-2 px-1 font-semibold " + (nativeMac ? "pl-20" : "")}>
        <img src={logoMark} alt="" className="size-5 rounded" /> 景渊
      </div>
      <button type="button" onClick={onSearch} onDoubleClick={(event) => event.stopPropagation()} style={noDrag}
        className="flex h-7 w-[min(44vw,440px)] items-center gap-2 rounded-md border bg-background px-2 text-left text-xs text-muted-foreground hover:border-[var(--border-emphasized)]">
        <Search className="size-3.5" /> <span className="flex-1 truncate">搜索股票与指令</span>
        <kbd className="rounded border px-1 text-[11px]">{isMacKey ? "⌘ K" : "Ctrl K"}</kbd>
      </button>
      <div className="flex min-w-0 items-center gap-1" style={noDrag} onDoubleClick={(event) => event.stopPropagation()}>
        <div className="hidden min-w-0 px-1 sm:block">{modelSelector}</div>
        <span title={health === false ? "服务未连接" : health ? "服务已连接" : "正在连接服务"} className="px-1" aria-live="polite">
          {health === false ? <WifiOff className="size-4 text-up" /> : <Wifi className={"size-4 " + (health ? "text-down" : "text-muted-foreground")} />}
        </span>
        <ThemeToggle />
        {!win && authRequired && (
          <button
            type="button"
            title="退出当前访问会话"
            aria-label="退出登录"
            onClick={() => {
              api.webAuthLogout().catch(() => {}).finally(() => {
                window.dispatchEvent(new Event("jingyuan:auth-required"));
              });
            }}
            className="grid size-7 place-items-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <LogOut className="size-3.5" />
          </button>
        )}
        {win && !nativeMac && <>
          <button type="button" aria-label="最小化" onClick={() => win.minimize()} className={iconButton}><Minus className="size-4" /></button>
          <button type="button" aria-label={maximized ? "还原" : "最大化"} onClick={() => win.maximizeToggle()} className={iconButton}>{maximized ? <Copy className="size-3" /> : <Square className="size-3" />}</button>
          <button type="button" aria-label="关闭" onClick={() => win.close()} className={iconButton + " hover:bg-destructive hover:text-destructive-foreground"}><X className="size-4" /></button>
        </>}
      </div>
    </header>
  );
}
