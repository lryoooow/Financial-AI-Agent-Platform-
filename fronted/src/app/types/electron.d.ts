// Electron preload 暴露的窗口控制 API 类型声明。
// 仅在桌面（Electron）运行时存在；浏览器运行时 window.finresearch 为 undefined，
// TitleBar 用 feature-detection 优雅降级（按钮可视但不触发 IPC）。
export {};

declare global {
  interface FinResearchWindow {
    minimize: () => void;
    maximizeToggle: () => void;
    close: () => void;
    isMaximized: () => Promise<boolean>;
    onMaximizeChange: (cb: (isMaximized: boolean) => void) => () => void;
  }

  /** v1.8.0：桌面偏好（托盘驻留 / 开机自启），由主进程持久化 */
  interface FinResearchDesktopPrefs {
    closeToTray: boolean;
    autoLaunch: boolean;
    version: string;
  }

  interface FinResearchApp {
    getPreferences: () => Promise<FinResearchDesktopPrefs>;
    setPreferences: (p: Partial<Pick<FinResearchDesktopPrefs, "closeToTray" | "autoLaunch">>) => Promise<FinResearchDesktopPrefs>;
  }

  interface FinResearchClipboard {
    writeImageDataUrl: (dataUrl: string) => Promise<boolean>;
  }

  interface Window {
    finresearch?: {
      platform: string;
      window: FinResearchWindow;
      app?: FinResearchApp;
      clipboard?: FinResearchClipboard;
    };
  }
}
