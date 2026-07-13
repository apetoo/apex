import { ArrowUpRight, ArrowDownRight, Target, ShieldAlert, X, Sparkles, Pencil } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { ActivePosition } from "@/api/watchlist";

/**
 * <CompactPositionCard> - 竖向紧凑持仓卡(网格用)
 *
 * 替代旧行式 PositionCard。一行 2-3 张排列。
 * 信息层次: 当前价(黑色加粗, PriceTag) -> 涨跌(红/绿) -> 持仓盈亏(红/绿)
 *          -> 参数行(股数@成本 / 止损/目标 灰小字) -> [加仓][减仓](可选)
 *
 * 编辑按钮放右上角(icon-only Pencil), 不挤占操作行。
 * onEdit/onAdd/onReduce 任一存在才渲染操作行;都不传 = 只读(概览页用)。
 * 红涨绿跌铁律: 盈亏 = (当前价 - avg_cost) * 股数。
 */
export function CompactPositionCard({
  position,
  onEdit,
  onAdd,
  onReduce,
  onClose,
  onSyncAi,
  syncing,
}: {
  position: ActivePosition;
  /** 人工编辑交易参数(止损/止盈/触发/过期/strategy 等)。PATCH 部分覆盖。右上角入口。 */
  onEdit?: (p: ActivePosition) => void;
  onAdd?: (p: ActivePosition) => void;
  onReduce?: (p: ActivePosition) => void;
  onClose?: (p: ActivePosition) => void;
  /** 同步最近 AI 分析的止损/目标(覆盖)。通常手动持仓缺 advice 时显示。 */
  onSyncAi?: (p: ActivePosition) => void;
  syncing?: boolean;
}) {
  const { ts_code, name, entry_price, avg_cost, stop_loss, target, position_size_shares, strategy } =
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

  const cost = avg_cost ?? entry_price;
  const pnl =
    currentPrice != null && position_size_shares != null
      ? (currentPrice - cost) * position_size_shares
      : null;
  const pnlDelta = currentPrice != null ? currentPrice - cost : null;

  return (
    <div className="rounded-lg border border-border bg-bg-card p-3">
      {/* 名称 + 策略 tag + 编辑(右上角 icon-only) */}
      <div className="flex items-center gap-2">
        <p className="truncate font-medium">{name}</p>
        {strategy && (
          <span className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-text-secondary">
            {strategy}
          </span>
        )}
        {onEdit && (
          <button
            type="button"
            onClick={() => onEdit(position)}
            className="ml-auto inline-flex items-center justify-center rounded p-1 text-text-secondary hover:bg-bg-base"
            title="编辑止损/止盈/触发/过期等交易参数"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>

      {/* 当前价(黑色) + 涨跌(红/绿) */}
      <div className="mt-2">
        <PriceTag
          price={currentPrice}
          prevClose={prevClose}
          loading={loading}
          error={error}
          size="lg"
          showChange
        />
      </div>

      {/* 持仓盈亏 */}
      {pnl != null && (
        <div
          className={cn(
            "num mt-1 flex items-center gap-0.5 text-xs",
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
            ({pnlDelta != null ? ((pnlDelta / cost) * 100).toFixed(2) : "-"}%)
          </span>
        </div>
      )}

      <div className="my-2 border-t border-border" />

      {/* 参数行: 股数@成本 / 止损 / 目标 */}
      <div className="space-y-1 text-[11px] text-text-secondary">
        <div className="num">
          {position_size_shares != null && <span>{position_size_shares} 股</span>}
          {avg_cost != null && <span> @ {formatPrice(avg_cost)}</span>}
          {position_size_shares == null && avg_cost == null && <span>-</span>}
        </div>
        {(stop_loss != null || target != null) && (
          <div className="num flex items-center gap-3">
            {stop_loss != null && (
              <span className="flex items-center gap-0.5">
                <ShieldAlert className="h-3 w-3 text-down" />
                {formatPrice(stop_loss)}
              </span>
            )}
            {target != null && (
              <span className="flex items-center gap-0.5">
                <Target className="h-3 w-3 text-up" />
                {formatPrice(target)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* 操作按钮(可选): 加仓/减仓/同步AI/平仓 */}
      {(onAdd || onReduce || onClose || onSyncAi) && (
        <div className="mt-2 flex items-center gap-1.5">
          {onAdd && (
            <button
              type="button"
              onClick={() => onAdd(position)}
              className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
            >
              加仓
            </button>
          )}
          {onReduce && (
            <button
              type="button"
              onClick={() => onReduce(position)}
              className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
            >
              减仓
            </button>
          )}
          {onSyncAi && (
            <button
              type="button"
              onClick={() => onSyncAi(position)}
              disabled={syncing}
              className="inline-flex items-center gap-0.5 rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base disabled:opacity-50"
              title="用最近一次 AI 分析的止损/目标覆盖当前值"
            >
              <Sparkles className="h-3 w-3" />
              {syncing ? "同步中..." : "同步AI"}
            </button>
          )}
          {onClose && (
            <button
              type="button"
              onClick={() => onClose(position)}
              className="ml-auto inline-flex items-center gap-0.5 rounded border border-down/30 px-2 py-0.5 text-[11px] text-down hover:bg-down/5"
            >
              <X className="h-3 w-3" />
              平仓
            </button>
          )}
        </div>
      )}
    </div>
  );
}
