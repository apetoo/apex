import { useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * <Dialog> — 居中模态
 *
 * 复用于: 加候选确认弹窗等需要用户确认的短表单场景。
 * 半透明遮罩 click 关闭 + ESC 关闭 + 打开时锁 body scroll。
 * 模式与 <Drawer> 对齐, 纯 tailwind, 不引入 headless UI。
 */
export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  /** panel 宽度类, 默认 max-w-md w-full */
  widthClass?: string;
  /** 提交中时禁止关闭, 避免半态。默认 false */
  busy?: boolean;
}

export function Dialog({ open, onClose, title, children, widthClass, busy }: DialogProps) {
  // ESC 关闭(busy 时禁用)
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, busy]);

  // 锁 body scroll
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  return (
    <div
      className={cn(
        "fixed inset-0 z-50 flex items-center justify-center transition-opacity",
        open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0",
      )}
      aria-hidden={!open}
    >
      {/* 遮罩 */}
      <div
        className="absolute inset-0 bg-black/30"
        onClick={() => !busy && onClose()}
      />

      {/* panel */}
      <div
        className={cn(
          "relative flex max-h-[90vh] w-full flex-col rounded-lg bg-bg-card shadow-xl transition-transform duration-200",
          widthClass ?? "max-w-md",
          open ? "scale-100" : "scale-95",
        )}
        role="dialog"
        aria-modal="true"
      >
        {title !== undefined && (
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div className="min-w-0 flex-1 text-sm font-medium text-text-primary">
              {title}
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="ml-2 rounded p-1 text-text-secondary hover:bg-bg-base hover:text-text-primary disabled:opacity-50"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}
        <div className="flex-1 overflow-y-auto px-4 py-4">{children}</div>
      </div>
    </div>
  );
}
