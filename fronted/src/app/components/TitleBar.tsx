import { useEffect, useState, type CSSProperties } from "react";
import { motion } from "motion/react";
import { Wifi, Minus, Square, Copy, X } from "lucide-react";
import { ThemeToggle } from "./ThemeToggle";
import logoMark from "../assets/logo-mark.svg";

/**
 * Windows 11 风格应用内标题栏（Electron frameless 窗口的唯一标题栏）。
 * 左：品牌 Logo；中：连接/交易状态 + 时钟；右：主题切换 + 最小化/最大化/关闭。
 *
 * 根因 2 修复：
 * - 整条标题栏 `-webkit-app-region: drag` 可拖拽移动窗口；
 * - 右侧控制按钮区 `-webkit-app-region: no-drag`，点击触发 IPC 真控窗口；
 * - 浏览器运行态（非 Electron）下 window.finresearch 不存在，按钮降级为可视不触发。
 */
export function TitleBar() {
  const [now, setNow] = useState(new Date());
  const [maximized, setMaximized] = useState(false);
  const win = typeof window !== "undefined" ? window.finresearch?.window : undefined;

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // 初始最大化状态 + 订阅变化（Electron 主进程 push）
  useEffect(() => {
    if (!win) return;
    let unsub: (() => void) | undefined;
    win.isMaximized().then(setMaximized).catch(() => {});
    unsub = win.onMaximizeChange(setMaximized);
    return () => unsub?.();
  }, [win]);

  // 交易时段判断（09:30–11:30, 13:00–15:00 工作日）
  const mins = now.getHours() * 60 + now.getMinutes();
  const day = now.getDay();
  const trading = day >= 1 && day <= 5 && ((mins >= 570 && mins <= 690) || (mins >= 780 && mins <= 900));

  const onDouble = () => win?.maximizeToggle();  // 双击标题栏切换最大化（Windows 习惯）

  return (
    <div
      className="h-10 shrink-0 flex items-center justify-between pl-3 border-b border-border bg-sidebar/80 backdrop-blur-xl select-none"
      style={{ WebkitAppRegion: "drag" } as CSSProperties}
      onDoubleClick={onDouble}
    >
      {/* 左：品牌 */}
      <div className="flex items-center gap-2 min-w-0">
        <img src={logoMark} alt="景渊" className="size-5 rounded-[5px] shrink-0" />
        <span className="text-sm text-foreground/90 leading-none whitespace-nowrap">
          <span className="font-display" style={{ fontWeight: 600 }}>景渊</span>
          <span className="text-muted-foreground ml-1.5 text-xs">金融研究助手</span>
        </span>
      </div>

      {/* 中：状态 */}
      <div className="hidden md:flex items-center gap-4 text-xs text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <Wifi className="size-3.5 text-down" />
          <span>本地服务 · 已连接</span>
        </div>
        <div className="flex items-center gap-1.5">
          <motion.span
            className="size-1.5 rounded-full"
            style={{ background: trading ? "var(--down)" : "var(--muted-foreground)" }}
            animate={trading ? { opacity: [1, 0.3, 1] } : {}}
            transition={{ repeat: Infinity, duration: 1.6 }}
          />
          <span>{trading ? "交易中" : "已休市"}</span>
        </div>
        <span className="tabular text-foreground/70">
          {now.toLocaleTimeString("zh-CN", { hour12: false })}
        </span>
      </div>

      {/* 右：主题切换 + Windows 窗口控制（no-drag，真实 IPC 控窗） */}
      <div
        className="flex items-center"
        style={{ WebkitAppRegion: "no-drag" } as CSSProperties}
      >
        <ThemeToggle />
        <button
          type="button"
          aria-label="最小化"
          title="最小化"
          onClick={() => win?.minimize()}
          className="grid place-items-center h-10 w-11 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <Minus className="size-4" />
        </button>
        <button
          type="button"
          aria-label={maximized ? "还原" : "最大化"}
          title={maximized ? "向下还原" : "最大化"}
          onClick={() => win?.maximizeToggle()}
          className="grid place-items-center h-10 w-11 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          {maximized ? <Copy className="size-3" /> : <Square className="size-3" />}
        </button>
        <button
          type="button"
          aria-label="关闭"
          title="关闭"
          onClick={() => win?.close()}
          className="grid place-items-center h-10 w-12 text-muted-foreground hover:bg-[#c42b1c] hover:text-white transition-colors"
        >
          <X className="size-4" />
        </button>
      </div>
    </div>
  );
}
