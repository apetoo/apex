import { ArrowUpRight, ArrowDownRight, Target, ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { ActivePosition } from "@/api/watchlist";

/**
 * <PositionCard> — 持仓行卡片
 *
 * 借鉴 21st.dev StockHoldingItem(divide-y 列表项 + 头像/名字/价/涨跌)结构,
 * 翻成 A 股红涨绿跌 + 加 A 股专属:
 *   - 止损价(target) + 目标价(shield) 标注
 *   - 距入场价的盈亏金额(若知道股数)
 *   - 策略归属(strategy tag)
 *
 * 红涨绿跌铁律: 当前价 vs entry_price 决定盈亏方向。
 * 字段语义见 apex/watchlist.py:add_position。
 */
export function PositionCard({ position }: { position: ActivePosition }) {
  const { ts_code, name, entry_price, stop_loss, target, position_size_shares, strategy } =
    position;

  const prices = useQuery({
    queryKey: qk.prices([ts_code]),
    queryFn: () => getPrices([ts_code]),
  });
  const daily = useQuery({
    queryKey: qk.dailyPrices([ts_code]),
    queryFn: () => getDailyPrices([ts_code]),
  });

  const currentPrice = prices.data?.[ts_code] ?? null;
  const prevClose = daily.data?.[ts_code] ?? null;
  const loading = prices.isLoading || daily.isLoading;
  const error = prices.isError || daily.isError;

  // 盈亏 = (当前价 - 入场价) * 股数
  const pnl =
    currentPrice != null && position_size_shares != null
      ? (currentPrice - entry_price) * position_size_shares
      : null;
  const pnlDelta = currentPrice != null ? currentPrice - entry_price : null;

  return (
    <div className="flex items-center justify-between gap-4 border-b border-border py-4 last:border-0">
      {/* 左侧: 名称 + 代码 + 策略 tag */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate font-medium">{name}</p>
          {strategy && (
            <span className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-text-secondary">
              {strategy}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>
        <div className="mt-1.5 flex items-center gap-3 text-[11px] text-text-secondary">
          <span className="flex items-center gap-0.5">
            <ShieldAlert className="h-3 w-3 text-down" />
            止损 {formatPrice(stop_loss)}
          </span>
          <span className="flex items-center gap-0.5">
            <Target className="h-3 w-3 text-up" />
            目标 {formatPrice(target)}
          </span>
        </div>
      </div>

      {/* 右侧: 价格 + 盈亏 */}
      <div className="text-right">
        <PriceTag
          price={currentPrice}
          prevClose={prevClose}
          loading={loading}
          error={error}
          size="md"
          showChange
        />
        {pnl != null && (
          <div
            className={cn(
              "mt-1 flex items-center justify-end gap-0.5 num text-xs",
              pnl >= 0 ? "text-up" : "text-down",
            )}
          >
            {pnl >= 0 ? (
              <ArrowUpRight className="h-3 w-3" />
            ) : (
              <ArrowDownRight className="h-3 w-3" />
            )}
            <span>
              {pnl >= 0 ? "+" : ""}
              {pnl.toFixed(0)} 元
            </span>
            <span className="ml-1 opacity-80">
              ({pnlDelta != null ? ((pnlDelta / entry_price) * 100).toFixed(2) : "—"}%)
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
