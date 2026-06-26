import { Bell, TrendingDown, TrendingUp, Target, ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { Candidate } from "@/api/watchlist";

/**
 * <CandidateCard> — 候选行
 *
 * 触发价 + 触发方向(下方/上方) + 当前价 → 是否触发的视觉提示
 * ED1: 距触发价的距离用「差值/百分比」显示
 * 红涨绿跌铁律: 当前价 vs 触发价的方向反一下(若是 below 触发, 当前越低越接近)
 */
export function CandidateCard({ candidate }: { candidate: Candidate }) {
  const { ts_code, name, trigger_price, trigger_direction, note, stop_advice, target_advice } =
    candidate;

  const prices = useQuery({
    queryKey: qk.prices([ts_code]),
    queryFn: () => getPrices([ts_code]),
  });

  const currentPrice = prices.data?.[ts_code] ?? null;
  const loading = prices.isLoading;
  const error = prices.isError;

  // 距触发价的距离
  const distance =
    currentPrice != null ? currentPrice - trigger_price : null;
  const distancePct =
    currentPrice != null && trigger_price !== 0
      ? (distance! / trigger_price) * 100
      : null;

  // 触发判断: below 触发 → 当前价 <= 触发价; above → 当前价 >= 触发价
  const isTriggered =
    currentPrice != null &&
    (trigger_direction === "below"
      ? currentPrice <= trigger_price
      : currentPrice >= trigger_price);

  const TrendingIcon = trigger_direction === "below" ? TrendingDown : TrendingUp;

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-4 border-b border-border py-4 last:border-0",
        isTriggered && "bg-up/5",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate font-medium">{name}</p>
          {isTriggered && (
            <span className="inline-flex items-center gap-0.5 rounded bg-up/10 px-1.5 py-0.5 text-[10px] font-medium text-up">
              <Bell className="h-2.5 w-2.5" /> 已触发
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>
        <p className="mt-1 line-clamp-1 text-[11px] text-text-secondary">{note}</p>
        <div className="mt-1.5 flex items-center gap-3 text-[11px] text-text-secondary">
          <span className="flex items-center gap-0.5">
            <TrendingIcon className="h-3 w-3" />
            触发 {formatPrice(trigger_price)}
          </span>
          {stop_advice > 0 && (
            <span className="flex items-center gap-0.5">
              <ShieldAlert className="h-3 w-3 text-down" />
              建议止损 {formatPrice(stop_advice)}
            </span>
          )}
          {target_advice > 0 && (
            <span className="flex items-center gap-0.5">
              <Target className="h-3 w-3 text-up" />
              目标 {formatPrice(target_advice)}
            </span>
          )}
        </div>
      </div>

      <div className="text-right">
        <PriceTag
          price={currentPrice}
          prevClose={null}
          loading={loading}
          error={error}
          size="md"
          showChange={false}
        />
        {distance != null && distancePct != null && !isTriggered && (
          <p className="num mt-1 text-xs text-text-secondary">
            距触发 {distance >= 0 ? "+" : ""}
            {distance.toFixed(2)}({distancePct >= 0 ? "+" : ""}
            {distancePct.toFixed(2)}%)
          </p>
        )}
      </div>
    </div>
  );
}
