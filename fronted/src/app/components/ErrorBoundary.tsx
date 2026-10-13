import { Component, type ReactNode } from "react";

interface Props { children: ReactNode; }
interface State { error: Error | null; }

/** 全局错误边界：frameless 窗口内 React 渲染异常 = 整页白屏且无路可逃（无原生标题栏）。
 * 包一层后至少给用户一个"重载页面"按钮自救。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: unknown) {
    console.error("ErrorBoundary:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="grid h-full place-items-center p-8">
          <div className="max-w-md text-center">
            <div className="mx-auto mb-4 grid size-14 place-items-center rounded-2xl bg-up/10">
              <span className="text-2xl">⚠️</span>
            </div>
            <h2 className="text-lg font-semibold">页面出现异常</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              {this.state.error.message || "渲染过程中发生未知错误"}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="mt-4 rounded-lg bg-gold px-4 py-2 text-sm text-[#17140a] hover:brightness-110"
            >
              重载页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
