import { useEffect, useRef, useState } from "react";
import { Loader2, AlertCircle, RefreshCw, Square, ChevronRight } from "lucide-react";
import { Card, CardContent, CardHeader, Button } from "@/components/base";
import { useSSE } from "@/hooks/useSSE";
import { analyzeFetchFn, analyzeRunPath } from "@/api/analyze";
import { cn } from "@/lib/utils";
import { TraceEventList, type TraceEvent } from "./TraceEventList";

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
 *   assistant_text — 主区, 多行(markdown)
 *   verdict        — verdict 标签(VerdictTag)
 *   error          — 红色错误
 *
 * UX:
 *   - **自动开始**: 挂载即 connect, 不需要点按钮(组件由 key=tsCode 控制重挂)。
 *   - **过程默认折叠**: 过程是给「想看细节」的人, 默认收起; 头部显示步数 + 状态。
 *   - ED1 重连策略: 都关自动重连; status=disconnected 头部显示「重试」。
 */

export interface AnalyzeTraceStreamProps {
  tsCode: string;
  onVerdict?: (verdict: Record<string, unknown>) => void;
  className?: string;
}

export function AnalyzeTraceStream({ tsCode, onVerdict, className }: AnalyzeTraceStreamProps) {
  const { events, status, error, result, connect, abort, reset } = useSSE<TraceEvent>();
  const [flushVersion, setFlushVersion] = useState(0);
  // 过程默认折叠
  const [expanded, setExpanded] = useState(false);

  // rAF 节流: 累积 events, 下一帧再 setState
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (events.length === 0) return;
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

  // 不自动开始 —— 由头部「开始分析」按钮触发(用户要求: 输入只展示下方内容, 点按钮才分析)。
  const start = () => {
    void connect({
      path: analyzeRunPath(tsCode, true),
      method: "GET",
      fetchFn: analyzeFetchFn(),
    });
  };

  const stop = () => abort();

  const retry = () => {
    reset();
    start();
  };

  const running = status === "connecting" || status === "streaming";

  const statusLabel = running
    ? "分析中…"
    : status === "done"
      ? "完成"
      : status === "disconnected"
        ? "断开"
        : "待开始";

  return (
    <Card className={className}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-1.5 text-sm font-normal text-text-secondary hover:text-text-primary"
        >
          <ChevronRight
            className={cn("h-3.5 w-3.5 transition-transform", expanded && "rotate-90")}
          />
          <span>分析过程</span>
          {events.length > 0 && (
            <span className="num text-xs text-flat">{events.length} 步</span>
          )}
          <span
            className={cn(
              "ml-1 text-xs",
              running
                ? "text-text-secondary"
                : status === "disconnected"
                  ? "text-down"
                  : "text-flat",
            )}
          >
            · {statusLabel}
          </span>
        </button>
        <div className="flex items-center gap-2">
          {running ? (
            <Button variant="ghost" size="sm" onClick={stop}>
              <Square className="mr-1 h-3.5 w-3.5" />
              中断
            </Button>
          ) : status === "disconnected" ? (
            <Button variant="primary" size="sm" onClick={retry}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重试
            </Button>
          ) : status === "done" ? (
            <Button variant="outline" size="sm" onClick={retry}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重跑
            </Button>
          ) : (
            <Button variant="primary" size="sm" onClick={start}>
              开始分析
            </Button>
          )}
        </div>
      </CardHeader>

      {/* 折叠态: 头部一行提示即可; 展开态: 完整事件流 */}
      {expanded && (
        <CardContent className="pt-0">
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

          {events.length === 0 && !error && (
            <p className="py-6 text-center text-sm text-flat">尚未收到事件…</p>
          )}

          {/* 事件流(rAF 节流, 一次性渲染) —— 剥离 SSE event 外壳, 复用 TraceEventList */}
          <div data-flush={flushVersion}>
            <TraceEventList events={events.map((e) => e.data as TraceEvent)} />

            {running && (
              <div className="mt-1.5 flex items-center gap-2 px-2 py-1.5 text-flat">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span className="text-[10px]">thinking...</span>
              </div>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
