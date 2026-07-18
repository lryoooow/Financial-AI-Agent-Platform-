import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";

/** 明暗主题切换（next-themes 驱动，给 <html> 加/去 .dark）*/
export function ThemeToggle({ className = "" }: { className?: string }) {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const isDark = resolvedTheme === "dark";

  return (
    <button
      type="button"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={isDark ? "切换到浅色主题" : "切换到深色主题"}
      title={isDark ? "浅色" : "深色"}
      className={`grid place-items-center size-8 rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors ${className}`}
    >
      {/* mounted 前渲染占位，避免主题未决时的图标闪烁 */}
      {mounted ? (
        isDark ? <Sun className="size-4" /> : <Moon className="size-4" />
      ) : (
        <span className="size-4" />
      )}
    </button>
  );
}
