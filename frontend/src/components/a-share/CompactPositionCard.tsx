import { ArrowUpRight, ArrowDownRight, Target, ShieldAlert, X, Sparkles, Pencil } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag, ScalePlanLadder, LatestAnalysisBadge } from "@/components/a-share";
import { getPrices, getPrevClosePrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice, formatAmount } from "@/lib/utils";
import type { ActivePosition, PositionPlan } from "@/api/watchlist";

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
  // 昨收用 prev-close(严格排除今天)。/prices/daily 盘后 15:30 会返回今日收盘,
  // 当 prevClose 用会导致 PriceTag 涨跌幅盘后恒 0.00%。
  const daily = useQuery({
    queryKey: qk.prevClose([ts_code]),
    queryFn: () => getPrevClosePrices([ts_code]),
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
  // 持有金额(市值) = 现价 × 股数;跟成本价同列小字,让上面的盈亏百分比有基数参照。
  const marketValue =
    currentPrice != null && position_size_shares != null
      ? currentPrice * position_size_shares
      : null;

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

      {/* 最近 AI 分析徽标(hover 浮窗 / 点击 Drawer) */}
      <LatestAnalysisBadge
        tsCode={ts_code}
        stopBefore={position.plan?.last_stop_before ?? stop_loss}
      />

      <div className="my-2 border-t border-border" />

      {/* 参数行: 股数@成本 / 止损 / 目标 */}
      <div className="space-y-1 text-[11px] text-text-secondary">
        <div className="num">
          {position_size_shares != null && <span>{position_size_shares} 股</span>}
          {avg_cost != null && <span> @ {formatPrice(avg_cost)}</span>}
          {marketValue != null && (
            <span className="ml-1">· 市值 {formatAmount(marketValue)}</span>
          )}
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

      {/* v1.1.0: 持仓建议（现在 vs 未来）。持仓建议=header；现在=当前决策+止损变动(带方向)；未来=条件触发计划。 */}
      {position.plan?.last_action || position.plan?.scale_plan?.length ? (
        <div className="mt-2 space-y-1 border-t border-border pt-2 text-[10px] text-text-secondary">
          <div className="flex items-center justify-between">
            <span className="text-flat">持仓建议</span>
            {position.plan?.updated_at ? (
              <span className="opacity-60">{position.plan.updated_at.slice(5, 16)}</span>
            ) : null}
          </div>
          {position.plan?.last_action && (
            <LastAdviceRow plan={position.plan} stopLoss={position.stop_loss} />
          )}
          {position.plan?.scale_plan?.length ? (
            <ScalePlanLadder items={position.plan.scale_plan} title="未来触发计划（触及才执行）" />
          ) : null}
        </div>
      ) : null}

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

// v1.1.0: 最近 position_action 快照行（持仓卡"现在 vs 未来"的"现在"）。
// last_stop_before 优先（position_action 时的旧止损）；缺则回退当前 stop_loss。
// 止损带方向：多头 after>before=↑收紧(保护浮盈，红)；after<before=↓放宽(让空间，绿)。
const ACTION_LABELS: Record<string, string> = {
  hold: "持有",
  add: "加仓",
  trim: "减仓",
  exit: "清仓",
};

function LastAdviceRow({ plan, stopLoss }: { plan: PositionPlan; stopLoss?: number }) {
  const action = plan.last_action;
  if (!action) return null;
  const label = ACTION_LABELS[action] ?? action;
  const tone =
    action === "add" ? "text-up" : action === "trim" || action === "exit" ? "text-down" : "text-text-primary";
  const before = plan.last_stop_before ?? stopLoss;
  const after = plan.last_new_stop;
  const changed = after != null && before != null && after !== before;
  const tighten = changed && (after as number) > (before as number);
  const unchanged = after != null && before != null && after === before;
  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
      <span className="text-flat">现在</span>
      <span className={cn("font-medium", tone)}>{label}</span>
      {changed ? (
        <span className="opacity-70">
          止损 {formatPrice(before)}→{formatPrice(after)}
          <span className={cn("ml-0.5 font-medium", tighten ? "text-up" : "text-down")}>
            {tighten ? "↑收紧" : "↓放宽"}
          </span>
        </span>
      ) : after != null ? (
        <span className="opacity-70">
          止损 {formatPrice(after)}{unchanged ? " 维持" : ""}
        </span>
      ) : null}
    </div>
  );
}
