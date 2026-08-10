import { useEffect, useMemo, useRef, useState } from "react";
import { MessageCircle, X, Send, Square, AlertCircle, RefreshCw, Trash2, Wrench, Loader2, Check } from "lucide-react";
import { useSSE } from "@/hooks/useSSE";
import { useLocalStorage } from "@/hooks/useLocalStorage";
import { useChatContext } from "@/hooks/useChatContext";
import { chatFetchFn } from "@/api/chat";
import { Button, Markdown } from "@/components/base";
import { cn } from "@/lib/utils";

/**
 * <ChatPanel> — 全局浮动 chat 面板(任意路由可呼出)
 *
 * ED2: 用 ChatContextProvider.buildMessage 单次注入上下文(不是每条 message)。
 *      上下文变化时下次发消息重注; 否则不重复, 避免 history 膨胀。
 * ED1: 断流 + 手动重试(POST 丢 body, 不能自动重连)。
 *
 * UI: 右下角悬浮按钮 → 打开后全屏半透明遮罩 + 右侧抽屉(sheet 风格)。
 * 简化: 不引入 shadcn Sheet, 用 fixed 定位 + transition 自己实现(避免多一个依赖)。
 */

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

/** 工具调用事件派生状态(由 tool_call/tool_result 事件按 id 配对) */
type ToolCall = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: "calling" | "done" | "error";
  result?: string;
  chars?: number;
};

type ToolCallData = { id: string; name: string; args: Record<string, unknown> };
type ToolResultData = { id: string; name: string; result: string; ok: boolean; chars: number };
type ChatEventData =
  | { content: string }
  | ToolCallData
  | ToolResultData
  | Record<string, never>;

function isToolCallData(data: ChatEventData): data is ToolCallData {
  return (
    "id" in data &&
    typeof data.id === "string" &&
    "name" in data &&
    typeof data.name === "string" &&
    "args" in data &&
    typeof data.args === "object" &&
    data.args !== null
  );
}

function isToolResultData(data: ChatEventData): data is ToolResultData {
  return (
    "id" in data &&
    typeof data.id === "string" &&
    "name" in data &&
    typeof data.name === "string" &&
    "result" in data &&
    typeof data.result === "string" &&
    "ok" in data &&
    typeof data.ok === "boolean" &&
    "chars" in data &&
    typeof data.chars === "number"
  );
}

function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(" · ");
}

export function ChatPanel() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  // 历史持久化到 localStorage: 刷新/重开浏览器仍保留(streamingReply 临时态不持久化)
  const [history, setHistory] = useLocalStorage<ChatMsg[]>("apex.chat.history", []);
  const [streamingReply, setStreamingReply] = useState("");
  // 工具结果展开状态(按 tool_call id)。新轮发送时重置。
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const { buildMessage } = useChatContext();
  const { events, status, error, connect, abort, reset } = useSSE<ChatEventData>();

  // 从 tool_call/tool_result 事件派生工具调用列表(按 id 配对)
  const toolCalls: ToolCall[] = useMemo(() => {
    const map = new Map<string, ToolCall>();
    for (const e of events) {
      if (e.event === "tool_call") {
        if (!isToolCallData(e.data)) continue;
        const d = e.data;
        map.set(d.id, { id: d.id, name: d.name, args: d.args ?? {}, status: "calling" });
      } else if (e.event === "tool_result") {
        if (!isToolResultData(e.data)) continue;
        const d = e.data;
        const ex = map.get(d.id);
        if (ex) {
          ex.status = d.ok ? "done" : "error";
          ex.result = d.result;
          ex.chars = d.chars;
        }
      }
    }
    return Array.from(map.values());
  }, [events]);

  // 累积 chunk 事件 → streaming reply
  // 全量派生(不增量 append): useSSE 一次 reader.read() 可能批量追加多个 chunk,
  // 增量只取最后一个会把中间 chunk 丢掉(表现=少字/断续); done 事件追加时也会
  // 误把上一块再 append 一次(重复)。直接从 events 重算最稳。
  useEffect(() => {
    if (events.length === 0) return;
    const text = events
      .filter((e) => e.event === "chunk")
      .map((e) => (e.data as { content?: string })?.content ?? "")
      .join("");
    setStreamingReply(text);
  }, [events]);

  // 终态: 拼到 history。只依赖 status(避免 streamingReply 变化重复触发);
  // done 时从 events 全量重算, 避免读 streamingReply 旧值漏掉最后一批 chunk。
  useEffect(() => {
    if (status !== "done") return;
    const text = events
      .filter((e) => e.event === "chunk")
      .map((e) => (e.data as { content?: string })?.content ?? "")
      .join("");
    if (text) {
      setHistory((h) => [...h, { role: "assistant", content: text }]);
      setStreamingReply("");
    }
  }, [status, events]);

  // 自动滚到底
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history, streamingReply]);

  // ESC 关闭(去掉了全屏遮罩的 click-to-close, 改由 ESC + 关闭按钮)
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const running = status === "connecting" || status === "streaming";

  const send = async () => {
    if (!input.trim() || running) return;
    const userText = input.trim();
    setInput("");
    setExpandedTools({}); // 新轮重置工具展开态

    // ED2: 单次注入上下文(若变化)
    const finalMessage = buildMessage(userText);
    setHistory((h) => [...h, { role: "user", content: userText }]); // UI 显示原文, 注入在后端看

    await connect({
      path: "/api/chat/stream",
      method: "POST",
      body: { message: finalMessage, history: history.map((h) => ({ role: h.role, content: h.content })) },
      fetchFn: chatFetchFn(finalMessage, history),
    });
  };

  const clearChat = () => {
    abort();
    reset();
    setHistory([]);
    setStreamingReply("");
  };

  return (
    <>
      {/* 浮动按钮 */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          aria-label="打开 AI 对话"
          className="fixed bottom-6 right-6 z-50 flex h-12 w-12 items-center justify-center rounded-full bg-text-primary text-bg-card shadow-lg transition-transform hover:scale-105"
        >
          <MessageCircle className="h-5 w-5" />
        </button>
      )}

      {/* 抽屉 — 不遮背景: 无全屏遮罩, 用户可继续看/操作后面页面; ESC 或关闭按钮退出 */}
      {open && (
        <div className="fixed bottom-0 right-0 top-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-bg-card shadow-xl">
          {/* Header */}
          <header className="flex items-center justify-between border-b border-border px-4 py-3">
            <div>
              <h2 className="font-serif text-lg font-medium">AI 对话</h2>
              <p className="text-xs text-text-secondary">
                复盘助理 · ESC 关闭
              </p>
            </div>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={clearChat}
                aria-label="清空"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => setOpen(false)}
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          </header>

          {/* 消息区 */}
          <div className="flex-1 overflow-y-auto px-4 py-3">
            {history.length === 0 && !streamingReply && !running && (
              <p className="mt-12 text-center text-sm text-flat">
                输入问题, AI 会结合当前页面上下文回答
              </p>
            )}

            {history.map((m, i) => (
              <div
                key={i}
                className={cn(
                  "mb-3 flex",
                  m.role === "user" ? "justify-end" : "justify-start",
                )}
              >
                <div
                  className={cn(
                    "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                    m.role === "user"
                      ? "bg-text-primary text-bg-card"
                      : "bg-bg-base text-text-primary",
                  )}
                >
                  {m.role === "user" ? (
                    <p className="whitespace-pre-wrap">{m.content}</p>
                  ) : (
                    <Markdown>{m.content}</Markdown>
                  )}
                </div>
              </div>
            ))}

              {/* 思考中: running 但还没出 chunk 且没工具调用(stage1 工具决策期间 / stage2 首字前)。
                  一旦 tool_call 事件到达, 切换为下方工具卡片显示。 */}
              {running && !streamingReply && toolCalls.length === 0 && (
                <div className="mb-3 flex justify-start">
                  <div className="rounded-lg bg-bg-base px-3 py-2.5 text-sm text-text-secondary">
                    <span className="inline-flex items-center gap-1.5">
                      AI 正在思考
                      <span className="inline-flex gap-0.5">
                        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.3s]" />
                        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.15s]" />
                        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current" />
                      </span>
                    </span>
                  </div>
                </div>
              )}

              {/* 工具调用卡片: stage1 的 tool_call/tool_result live 显示, 在回复上方 */}
              {toolCalls.length > 0 && (running || streamingReply) && (
                <div className="mb-3 flex justify-start">
                  <div className="w-full max-w-[90%] rounded-lg border border-border bg-bg-base px-3 py-2">
                    <div className="mb-1.5 flex items-center gap-1.5 text-xs text-text-secondary">
                      <Wrench className="h-3 w-3" />
                      <span>调用 {toolCalls.length} 个工具</span>
                    </div>
                    <div className="space-y-1.5">
                      {toolCalls.map((tc) => (
                        <div key={tc.id} className="text-xs">
                          <div className="flex items-center gap-1.5">
                            {tc.status === "calling" ? (
                              <Loader2 className="h-3 w-3 animate-spin text-text-secondary" />
                            ) : tc.status === "error" ? (
                              // text-up=#e14b4b 红(错误); token 是价格语义但颜色符合状态常规
                              <AlertCircle className="h-3 w-3 text-up" />
                            ) : (
                              // text-down=#2ba84a 绿(完成)
                              <Check className="h-3 w-3 text-down" />
                            )}
                            <span className="font-mono text-text-primary">{tc.name}</span>
                            {formatArgs(tc.args) && (
                              <span className="font-mono text-text-secondary">{formatArgs(tc.args)}</span>
                            )}
                            {tc.status !== "calling" && tc.chars != null && (
                              <span className="text-flat">↳ {tc.chars} 字</span>
                            )}
                            {tc.result && (
                              <button
                                type="button"
                                onClick={() =>
                                  setExpandedTools((s) => ({ ...s, [tc.id]: !s[tc.id] }))
                                }
                                className="ml-auto text-text-secondary hover:text-text-primary"
                              >
                                {expandedTools[tc.id] ? "收起" : "展开"}
                              </button>
                            )}
                          </div>
                          {expandedTools[tc.id] && tc.result && (
                            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-bg-card p-2 font-mono text-[11px] leading-relaxed text-text-secondary">
                              {tc.result}
                            </pre>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {streamingReply && (
                <div className="mb-3 flex justify-start">
                  <div className="max-w-[85%] rounded-lg bg-bg-base px-3 py-2 text-sm text-text-primary">
                    <Markdown>{streamingReply}</Markdown>
                    <span className="mt-1 inline-block h-3 w-1 animate-pulse bg-text-secondary align-middle" />
                  </div>
                </div>
              )}

              {error && (
                <div className="mb-3 flex items-start gap-2 rounded border border-down/20 bg-down/5 p-2.5">
                  <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-down" />
                  <div className="flex-1">
                    <p className="text-xs text-down">连接断开 · {error}</p>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="mt-1 h-7 px-2"
                      onClick={() => {
                        if (history.length > 0) {
                          const lastUser = [...history]
                            .reverse()
                            .find((m) => m.role === "user");
                          if (lastUser) {
                            setHistory((h) => h.slice(0, -1));
                            setInput(lastUser.content);
                            setStreamingReply("");
                            reset();
                          }
                        }
                      }}
                    >
                      <RefreshCw className="mr-1 h-3 w-3" />
                      重试
                    </Button>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* 输入区 */}
            <footer className="border-t border-border px-4 py-3">
              <div className="flex items-end gap-2">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  placeholder="输入问题 (Enter 发送, Shift+Enter 换行)"
                  rows={2}
                  className="flex-1 resize-none rounded-md border border-border bg-bg-card px-3 py-2 text-sm placeholder:text-flat focus:border-text-secondary focus:outline-none"
                  disabled={running}
                />
                {running ? (
                  <Button variant="ghost" size="icon" onClick={abort} aria-label="中断">
                    <Square className="h-4 w-4" />
                  </Button>
                ) : (
                  <Button variant="primary" size="icon" onClick={send} disabled={!input.trim()} aria-label="发送">
                    <Send className="h-4 w-4" />
                  </Button>
                )}
              </div>
            </footer>
        </div>
      )}
    </>
  );
}
