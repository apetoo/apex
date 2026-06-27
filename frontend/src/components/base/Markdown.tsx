import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/**
 * <Markdown> — AI 文本 markdown 渲染
 *
 * AI 分析结论(analysis_text) / chat 回复 / trace assistant_text 都含 markdown
 * (标题 / **加粗** / 列表 / `代码` / > 引用 / 表格)。原来用 whitespace-pre-wrap
 * 纯文本, 格式全丢。这里用 react-markdown + remark-gfm(GFM 表格/删除线/任务列表)。
 *
 * 不依赖 @tailwindcss/typography: 用 components prop 手动给元素加 className,
 * 风格贴合雪球(克制字号 / 红涨绿跌不染色正文)。
 */
export interface MarkdownProps {
  children: string;
  className?: string;
  /** 紧凑模式(trace 行内, 缩小字号/间距) */
  compact?: boolean;
}

export function Markdown({ children, className, compact = false }: MarkdownProps) {
  return (
    <div
      className={cn(
        "text-text-primary",
        compact ? "text-xs leading-relaxed" : "text-sm leading-relaxed",
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h1 className={cn("font-semibold tracking-tight", compact ? "mt-2 mb-1 text-sm" : "mt-4 mb-2 text-lg")}>{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className={cn("font-semibold tracking-tight", compact ? "mt-2 mb-1 text-sm" : "mt-4 mb-2 text-base")}>{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className={cn("font-medium", compact ? "mt-1.5 mb-1 text-xs" : "mt-3 mb-1.5 text-sm")}>{children}</h3>
          ),
          h4: ({ children }) => (
            <h4 className={cn("font-medium", compact ? "mt-1 mb-0.5 text-xs" : "mt-2 mb-1 text-sm")}>{children}</h4>
          ),
          p: ({ children }) => (
            <p className={cn(compact ? "my-1" : "my-2")}>{children}</p>
          ),
          ul: ({ children }) => (
            <ul className={cn("list-disc space-y-0.5 pl-5", compact ? "my-1" : "my-2")}>{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className={cn("list-decimal space-y-0.5 pl-5", compact ? "my-1" : "my-2")}>{children}</ol>
          ),
          li: ({ children }) => <li className="pl-0.5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-text-primary">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          del: ({ children }) => <del className="text-flat">{children}</del>,
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer" className="text-text-secondary underline underline-offset-2 hover:text-text-primary">
              {children}
            </a>
          ),
          blockquote: ({ children }) => (
            <blockquote className={cn("border-l-2 border-border bg-bg-base px-3 text-text-secondary", compact ? "my-1 py-1 text-xs" : "my-2 py-2")}>
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-border" />,
          code: ({ className: cls, children, ...props }) => {
            const isBlock = /language-/.test(cls ?? "");
            if (isBlock) {
              return (
                <code className={cn("block overflow-x-auto rounded bg-bg-base p-2 font-mono text-xs", cls)} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code className="rounded bg-bg-base px-1 py-0.5 font-mono text-[0.85em] text-text-secondary" {...props}>
                {children}
              </code>
            );
          },
          pre: ({ children }) => <pre className="my-2">{children}</pre>,
          table: ({ children }) => (
            <div className="my-2 overflow-x-auto">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-bg-base">{children}</thead>,
          th: ({ children }) => (
            <th className="border border-border px-2 py-1 text-left font-medium text-text-secondary">{children}</th>
          ),
          td: ({ children }) => (
            <td className="border border-border px-2 py-1 text-text-primary">{children}</td>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
