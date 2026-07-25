import { cn, formatPrice } from "@/lib/utils";
import type { ScalePlanItem } from "@/api/watchlist";

/**
 * <ScalePlanLadder> - 加减仓 ladder 渲染（条件触发计划，非现役指令）
 *
 * 复用于: CompactPositionCard（紧凑，无 reason）+ VerdictDetailCard position_action 分支（带 reason）。
 * 标题强调"价格触及才执行"，避免被误读为当前加减仓指令——当前决策走 last_action，不在这里。
 * 红涨绿跌铁律：加=红(up)，减=绿(down)。
 */
export function ScalePlanLadder({
  items,
  title = "未来触发计划（价格触及才执行）",
  showReason = false,
}: {
  items: ScalePlanItem[];
  title?: string;
  showReason?: boolean;
}) {
  if (!items?.length) return null;
  return (
    <div className="space-y-0.5">
      <div className="text-[10px] text-text-secondary opacity-70">{title}</div>
      {items.map((item, i) => (
        <div key={i} className="text-[10px] text-text-secondary">
          <div className="num flex items-center gap-1">
            <span className="text-text-primary">@{formatPrice(item.trigger_price)}</span>
            <span
              className={cn(
                "rounded px-1",
                item.action === "trim" ? "bg-down/10 text-down" : "bg-up/10 text-up",
              )}
            >
              {item.action === "trim" ? "减" : "加"}
            </span>
            {item.shares != null && <span>{item.shares}股</span>}
            {item.pct != null && <span>{Math.round(item.pct * 100)}%</span>}
            {item.new_stop != null && <span className="opacity-70">止损→{formatPrice(item.new_stop)}</span>}
            {item.executed && <span className="opacity-50">✓</span>}
          </div>
          {showReason && item.reason && <div className="pl-1 opacity-70">{item.reason}</div>}
        </div>
      ))}
    </div>
  );
}
