import { useEffect, useRef, useState } from "react";
import { MessageCircle, X, Send, Square, AlertCircle, RefreshCw, Trash2 } from "lucide-react";
import { useSSE } from "@/hooks/useSSE";
import { useChatContext } from "@/hooks/useChatContext";
import { chatFetchFn } from "@/api/chat";
import { Button } from "@/components/base";
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

type ChunkEvent = { content: string } | Record<string, never>;

export function ChatPanel() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [history, setHistory] = useState<ChatMsg[]>([]);
  const [streamingReply, setStreamingReply] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const { buildMessage } = useChatContext();
  const { events, status, error, connect, abort, reset } = useSSE<ChunkEvent>();

  // 累积 chunk 事件 → streaming reply
  useEffect(() => {
    if (events.length === 0) return;
    const lastChunk = [...events]
      .reverse()
      .find((e) => e.event === "chunk");
    if (lastChunk) {
      const data = lastChunk.data as { content?: string };
      if (data?.content) setStreamingReply((s) => s + data.content!);
    }
  }, [events]);

  // 终态: 拼到 history
  useEffect(() => {
    if (status === "done" && streamingReply) {
      setHistory((h) => [...h, { role: "assistant", content: streamingReply }]);
      setStreamingReply("");
    }
  }, [status, streamingReply]);

  // 自动滚到底
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history, streamingReply]);

  const running = status === "connecting" || status === "streaming";

  const send = async () => {
    if (!input.trim() || running) return;
    const userText = input.trim();
    setInput("");

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

      {/* 抽屉 */}
      {open && (
        <>
          {/* 遮罩 */}
          <div
            className="fixed inset-0 z-40 bg-text-primary/20 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />
          {/* 面板 */}
          <div className="fixed bottom-0 right-0 top-0 z-50 flex w-full max-w-md flex-col border-l border-border bg-bg-card shadow-xl">
            {/* Header */}
            <header className="flex items-center justify-between border-b border-border px-4 py-3">
              <div>
                <h2 className="font-serif text-lg font-medium">AI 对话</h2>
                <p className="text-xs text-text-secondary">
                  ED2 上下文单次注入 · ED1 断流手动重试
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
              {history.length === 0 && !streamingReply && (
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
                    <p className="whitespace-pre-wrap">{m.content}</p>
                  </div>
                </div>
              ))}

              {streamingReply && (
                <div className="mb-3 flex justify-start">
                  <div className="max-w-[85%] rounded-lg bg-bg-base px-3 py-2 text-sm text-text-primary">
                    <p className="whitespace-pre-wrap">{streamingReply}</p>
                    <span className="mt-1 inline-block h-2 w-1 animate-pulse bg-text-secondary" />
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
        </>
      )}
    </>
  );
}
