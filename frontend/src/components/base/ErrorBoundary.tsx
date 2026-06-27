import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertCircle, RefreshCw } from "lucide-react";
import { Button } from "./button";

/**
 * <ErrorBoundary> — 单页错误兜底
 *
 * 不加 boundary 的话, 任意页面渲染抛错会让整个 App 白屏(React 卸载整棵树)。
 * 包在每个 <Route> element 上: 单页崩了只显示该页的重试 UI, 导航/其它 tab 不受影响。
 *
 * dev 仍会弹 React 的错误浮层(那是 Vite/React 故意叠的开发态辅助, 不影响生产);
 * 生产构建无浮层, 只显示这里的 fallback。
 */
interface Props {
  children: ReactNode;
  /** 用于 fallback 标识当前页, e.g. "粗筛" */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 不引入日志服务, 控制台留痕即可(单用户工具)
    console.error("[ErrorBoundary]", this.props.label ?? "", error, info);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-2xl px-6 py-16">
          <div className="flex flex-col items-center gap-4 rounded-lg border border-down/20 bg-down/5 p-8 text-center">
            <AlertCircle className="h-8 w-8 text-down" />
            <div>
              <p className="font-medium text-text-primary">
                {this.props.label ?? "页面"}渲染出错
              </p>
              <p className="mt-1 break-all font-mono text-xs text-text-secondary">
                {this.state.error.message}
              </p>
            </div>
            <Button variant="primary" onClick={this.reset}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重试
            </Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
