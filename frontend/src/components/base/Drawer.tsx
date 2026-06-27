import { useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * <Drawer> — 右侧滑出抽屉
 *
 * 用于列表点行看详情(journal 历史)等场景, 不跳页、不打断列表浏览。
 * 半透明遮罩 click 关闭 + ESC 关闭 + 打开时锁 body scroll。
 * 不引入 headless UI, 纯 tailwind + translate-x 过渡。
 */
export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  /** panel 宽度类, 默认 max-w-md w-full */
  widthClass?: string;
}

export function Drawer({ open, onClose, title, children, widthClass }: DrawerProps) {
  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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
        "fixed inset-0 z-50 transition-opacity",
        open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0",
      )}
      aria-hidden={!open}
    >
      {/* 遮罩 */}
      <div
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
      />

      {/* panel */}
      <div
        className={cn(
          "absolute right-0 top-0 flex h-full w-full flex-col bg-bg-card shadow-xl transition-transform duration-200",
          widthClass ?? "max-w-md",
          open ? "translate-x-0" : "translate-x-full",
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
              className="ml-2 rounded p-1 text-text-secondary hover:bg-bg-base hover:text-text-primary"
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
