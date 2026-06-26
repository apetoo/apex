import { cn } from "@/lib/utils";
import { verdictColor } from "@/types/verdict";

/**
 * <VerdictTag> — AI verdict 标签, 红涨绿跌映射
 *
 * 数据源: journal verdict 字段
 * 映射: types/verdict.ts(ED7: TS const, 同步自 apex/schemas.VERDICT_ENUM)
 *
 * 看多系 → up(红), 中性系 → flat(灰), 看空系 → down(绿)
 * 克制: 轻底色 + 文字色, 不整块染色
 */
export interface VerdictTagProps {
  verdict: string | null | undefined;
  className?: string;
}

const COLOR_STYLE: Record<string, string> = {
  up: "text-up bg-up/8 border-up/20",
  flat: "text-flat bg-flat/8 border-flat/20",
  down: "text-down bg-down/8 border-down/20",
};

export function VerdictTag({ verdict, className }: VerdictTagProps) {
  if (!verdict) {
    return (
      <span className={cn("inline-flex items-center rounded border px-1.5 py-0.5 text-xs text-flat border-flat/20 bg-flat/8", className)}>
        —
      </span>
    );
  }

  const color = verdictColor(verdict);

  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium",
        COLOR_STYLE[color],
        className,
      )}
    >
      {verdict}
    </span>
  );
}
