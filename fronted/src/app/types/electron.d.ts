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

  interface Window {
    finresearch?: {
      window: FinResearchWindow;
    };
  }
}
