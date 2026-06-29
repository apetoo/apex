import { Wrench, MessageSquare, FileText, CheckCircle2, XCircle } from "lucide-react";
import { Markdown } from "@/components/base";
import { cn } from "@/lib/utils";

/**
 * <TraceEventList> — trace 事件流渲染，live SSE 流式与静态回放(trace.jsonl)共用。
 *
 * 事件形状对齐真后端 `apex/analyze.py:_emit`（非 mock）：
 *   context        : {name, content}
 *   tool_call      : {iteration, name, args}
 *   tool_result    : {iteration, name, summary(dict), raw(JSON str)}   ← summary 优先, 降级 raw
 *   assistant_text : {iteration, content}
 *   verdict_recorded  : {iteration, verdict, confidence}                ← 真后端 verdict 事件
 *   verdict_rejected  : {iteration, missing, performed}                 ← 强制类别未齐, 被拒
 *
 * 兼容旧 mock 字段（result / type==="verdict"）—— dev mock 仍可展示，不破坏。
 */

export type TraceEvent = {
  type?: string;
  name?: string;
  iteration?: number;
  args?: Record<string, unknown>;
  content?: string;
  // 真后端 tool_result
  summary?: Record<string, unknown> | null;
  raw?: unknown;
  // 旧 mock tool_result（兼容）
  result?: unknown;
  // verdict（真后端 verdict_recorded / 旧 mock verdict）
  verdict?: string;
  confidence?: number;
  // verdict_rejected
  missing?: string[];
  performed?: string[];
  error?: string;
  [key: string]: unknown;
};

const EVENT_META: Record<string, { icon: typeof Wrench; label: string; tone: string }> = {
  context: { icon: FileText, label: "上下文", tone: "text-flat" },
  tool_call: { icon: Wrench, label: "调工具", tone: "text-flat" },
  tool_result: { icon: FileText, label: "工具结果", tone: "text-flat" },
  assistant_text: { icon: MessageSquare, label: "AI", tone: "text-text-primary" },
  verdict_recorded: { icon: CheckCircle2, label: "结论", tone: "text-text-primary" },
  verdict_rejected: { icon: XCircle, label: "被拒", tone: "text-down" },
  // 旧 mock 兼容
  verdict: { icon: CheckCircle2, label: "结论", tone: "text-text-primary" },
};

/** tool_result 的展示文本：优先 summary(dict)，降级 raw/旧 result，截断到 200 字。 */
function toolResultText(data: TraceEvent): string {
  const summary = data.summary;
  if (summary && typeof summary === "object" && Object.keys(summary).length > 0) {
    return JSON.stringify(summary).slice(0, 200);
  }
  const fallback = data.raw ?? data.result ?? {};
  const txt = typeof fallback === "string" ? fallback : JSON.stringify(fallback);
  return txt.slice(0, 200);
}

export interface TraceEventListProps {
  events: TraceEvent[];
  className?: string;
}

export function TraceEventList({ events, className }: TraceEventListProps) {
  return (
    <div className={cn("space-y-1.5 font-mono text-xs", className)}>
      {events.map((data, i) => {
        const meta = EVENT_META[data.type ?? ""] ?? EVENT_META.context;
        const Icon = meta.icon;
        const isVerdict = data.type === "verdict_recorded" || data.type === "verdict";
        const isRejected = data.type === "verdict_rejected";
        return (
          <div
            key={i}
            className={cn(
              "flex items-start gap-2 rounded px-2 py-1.5",
              isVerdict
                ? "bg-up/5 border border-up/20"
                : isRejected
                  ? "bg-down/5 border border-down/20"
                  : "hover:bg-bg-base",
            )}
          >
            <Icon className={cn("mt-0.5 h-3.5 w-3.5 flex-shrink-0", meta.tone)} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <span className={cn("text-[10px] font-medium uppercase", meta.tone)}>
                  {meta.label}
                </span>
                {data.iteration !== undefined && (
                  <span className="text-[10px] text-flat">iter {data.iteration}</span>
                )}
                {isVerdict && data.verdict && (
                  <span className="num text-sm font-semibold text-up">
                    {String(data.verdict)} · 置信度 {String(data.confidence)}
                  </span>
                )}
                {isRejected && (
                  <span className="num text-sm font-semibold text-down">
                    缺强制类别：{Array.isArray(data.missing) ? data.missing.join("/") : "—"}
                  </span>
                )}
              </div>
              {data.type === "assistant_text" && data.content && (
                <div className="mt-1 font-sans">
                  <Markdown compact>{String(data.content)}</Markdown>
                </div>
              )}
              {data.type === "tool_call" && data.name && (
                <p className="mt-0.5 text-text-secondary">
                  {String(data.name)}({JSON.stringify(data.args ?? {})})
                </p>
              )}
              {data.type === "tool_result" && data.name && (
                <p className="mt-0.5 line-clamp-2 text-flat">{toolResultText(data)}</p>
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
    </div>
  );
}
