import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 应用级错误边界（v1.6.0，审计 C4）。
 *
 * 此前整个应用没有 ErrorBoundary：Settings 等常挂载页任何一个渲染期 TypeError
 * 都会让 React 卸载整棵树 —— 用户看到的是无提示白屏。现在兜住渲染异常，
 * 给出可操作的错误卡片（错误摘要 + 重新加载），开发时可展开堆栈。
 */
interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  info: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // 不入网络上报：本地单用户应用，错误只留控制台与界面
    console.error("[ErrorBoundary]", error, errorInfo.componentStack);
    this.setState({ info: errorInfo.componentStack || "" });
  }

  render() {
    if (this.state.error) {
      return (
        <div className="size-full grid place-items-center bg-background text-foreground p-8">
          <div className="max-w-lg w-full rounded-xl border border-border bg-card p-6 space-y-4">
            <h1 className="font-display text-lg font-semibold">界面出现异常</h1>
            <p className="text-sm text-muted-foreground">
              页面渲染遇到未处理的错误。你的数据保存在本地数据库中，不会因此丢失；
              重新加载通常可以恢复。
            </p>
            <pre className="text-xs text-destructive whitespace-pre-wrap break-all max-h-40 overflow-y-auto bg-secondary/40 rounded-lg p-3">
              {this.state.error.message}
            </pre>
            <div className="flex gap-2">
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 rounded-lg bg-gold text-[#17140a] text-sm hover:brightness-110 transition-all"
              >
                重新加载
              </button>
              <button
                onClick={() => this.setState({ error: null, info: "" })}
                className="px-4 py-2 rounded-lg bg-secondary text-sm hover:bg-secondary/70 transition-colors"
              >
                尝试继续
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
