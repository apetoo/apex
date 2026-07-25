import { cn } from "@/lib/utils";

/**
 * <PositionActionTag> - position_action 的 action 徽章（持有/加仓/减仓/清仓）
 *
 * 历史列表行里与 <VerdictTag> 二选一：verdict entry 显示 VerdictTag，position_action entry
 * 显示本组件（verdict=None 不该显示空标签）。红涨绿跌：加=红(up)，减/清=绿(down)，持有=中性。
 */
const ACTION_META: Record<string, { label: string; tone: string }> = {
  hold: { label: "持有", tone: "text-text-primary" },
  add: { label: "加仓", tone: "text-up" },
  trim: { label: "减仓", tone: "text-down" },
  exit: { label: "清仓", tone: "text-down" },
};

export function PositionActionTag({ action }: { action: string }) {
  const meta = ACTION_META[action] ?? { label: action || "持仓建议", tone: "text-flat" };
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[11px] font-medium", meta.tone)}>{meta.label}</span>
  );
}
