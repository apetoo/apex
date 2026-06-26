import { useEffect, useRef, useState } from "react";
import { Wrench, MessageSquare, FileText, CheckCircle2, Loader2, AlertCircle, RefreshCw, Square } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Button } from "@/components/base";
import { useSSE } from "@/hooks/useSSE";
import { analyzeFetchFn } from "@/api/analyze";
import { cn } from "@/lib/utils";

/**
 * <AnalyzeTraceStream> — analyze SSE 流式 trace 渲染
 *
 * ED10 rAF 节流: trace 事件高频(几十条/秒), 直接 setState 会卡。
 * 用 useRef 累积事件 + requestAnimationFrame 批量 flush 到 state。
 *
 * 事件类型(analyze.run on_progress):
 *   context        — 灰色, 折叠
 *   tool_call      — 蓝色, 工具名
 *   tool_result    — 灰, 折叠
 *   assistant_text — 主区, 多行
 *   verdict        — verdict 标签(VerdictTag)
 *   error          — 红色错误
 *
 * ED1 重连策略: 都关自动重连; status=disconnected 显示「点击重试」按钮。
 */

type TraceEvent = {
  type?: string;
  name?: string;
  iteration?: number;
  args?: Record<string, unknown>;
  content?: string;
  result?: unknown;
  verdict?: string;
  confidence?: number;
  error?: string;
  // 终态 verdict 字段
  [key: string]: unknown;
};

const EVENT_META: Record<string, { icon: typeof Wrench; label: string; tone: string }> = {
  context: { icon: FileText, label: "上下文", tone: "text-flat" },
  tool_call: { icon: Wrench, label: "调工具", tone: "text-flat" },
  tool_result: { icon: FileText, label: "工具结果", tone: "text-flat" },
  assistant_text: { icon: MessageSquare, label: "AI", tone: "text-text-primary" },
  verdict: { icon: CheckCircle2, label: "结论", tone: "text-text-primary" },
};

export interface AnalyzeTraceStreamProps {
  tsCode: string;
  onVerdict?: (verdict: Record<string, unknown>) => void;
  className?: string;
}

export function AnalyzeTraceStream({ tsCode, onVerdict, className }: AnalyzeTraceStreamProps) {
  const { events, status, error, result, connect, abort, reset } = useSSE<TraceEvent>();
  const [flushVersion, setFlushVersion] = useState(0);

  // rAF 节流: 累积 events, 下一帧再 setState
  const rafRef = useRef<number | null>(null);

  // 事件到时累积 + 安排 flush
  useEffect(() => {
    if (events.length === 0) return;
    // 已经在 state 里, 此处不强加, 仅记录新增(由 hook 自管)
    // 但 useSSE 内部已 setState, 我们用 flushVersion 触发下游子组件用 ref 读
    if (rafRef.current === null) {
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        setFlushVersion((v) => v + 1);
      });
    }
  }, [events.length]);

  // 终态 verdict 回调
  useEffect(() => {
    if (result && onVerdict) onVerdict(result);
  }, [result, onVerdict]);

  const start = () => {
    void connect({
      path: `/analyze/run?ts_code=${encodeURIComponent(tsCode)}&save=true`,
      method: "GET",
      fetchFn: analyzeFetchFn(),
    });
  };

  const stop = () => {
    abort();
  };

  const retry = () => {
    reset();
    start();
  };

  const running = status === "connecting" || status === "streaming";

  return (
    <Card className={className}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-sm font-normal text-text-secondary">
          AI 分析 trace
        </CardTitle>
        <div className="flex items-center gap-2">
          {running ? (
            <Button variant="ghost" size="sm" onClick={stop}>
              <Square className="mr-1 h-3.5 w-3.5" />
              中断
            </Button>
          ) : status === "done" ? (
            <Button variant="outline" size="sm" onClick={retry}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重跑
            </Button>
          ) : status === "disconnected" ? (
            <Button variant="primary" size="sm" onClick={retry}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重试
            </Button>
          ) : (
            <Button variant="primary" size="sm" onClick={start}>
              开始分析
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {status === "idle" && events.length === 0 && (
          <p className="py-8 text-center text-sm text-flat">
            点击「开始分析」跑 AI 工具链(trace 流式渲染)
          </p>
        )}

        {/* ED1: 断流 + 手动重试(替代自动重连) */}
        {status === "disconnected" && error && (
          <div className="mb-3 flex items-start gap-2 rounded-md border border-down/20 bg-down/5 p-3">
            <AlertCircle className="mt-0.5 h-4 w-4 text-down" />
            <div className="flex-1">
              <p className="text-sm text-down">连接断开 · {error}</p>
              <p className="mt-1 text-xs text-text-secondary">
                ED1 不会自动重连(防二次扣费)。点「重试」手动重发。
              </p>
            </div>
          </div>
        )}

        {/* 事件流(rAF 节流, 一次性渲染) */}
        <div className="space-y-1.5 font-mono text-xs" data-flush={flushVersion}>
          {events.map((e, i) => {
            const data = e.data as TraceEvent;
            const meta = EVENT_META[data.type ?? ""] ?? EVENT_META.context;
            const Icon = meta.icon;
            const isVerdict = data.type === "verdict";
            return (
              <div
                key={i}
                className={cn(
                  "flex items-start gap-2 rounded px-2 py-1.5",
                  isVerdict ? "bg-up/5 border border-up/20" : "hover:bg-bg-base",
                )}
              >
                <Icon className={cn("mt-0.5 h-3.5 w-3.5 flex-shrink-0", meta.tone)} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className={cn("text-[10px] font-medium uppercase", meta.tone)}>
                      {meta.label}
                    </span>
                    {data.iteration !== undefined && (
                      <span className="text-[10px] text-flat">
                        iter {data.iteration}
                      </span>
                    )}
                    {isVerdict && data.verdict && (
                      <span className="num text-sm font-semibold text-up">
                        {String(data.verdict)} · 置信度 {String(data.confidence)}
                      </span>
                    )}
                  </div>
                  {data.type === "assistant_text" && data.content && (
                    <p className="mt-1 whitespace-pre-wrap text-text-primary">
                      {String(data.content)}
                    </p>
                  )}
                  {data.type === "tool_call" && data.name && (
                    <p className="mt-0.5 text-text-secondary">
                      {String(data.name)}({JSON.stringify(data.args ?? {})})
                    </p>
                  )}
                  {data.type === "tool_result" && data.name && (
                    <p className="mt-0.5 line-clamp-2 text-flat">
                      {JSON.stringify(data.result ?? {}).slice(0, 200)}
                    </p>
                  )}
                  {data.type === "context" && data.name && (
                    <p className="mt-0.5 line-clamp-1 text-flat">
                      {String(data.name)}: {String(data.content ?? "").slice(0, 100)}
                    </p>
                  )}
                </div>
              </div>
            );
          })}

          {running && (
            <div className="flex items-center gap-2 px-2 py-1.5 text-flat">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span className="text-[10px]">thinking...</span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
