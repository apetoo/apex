import type { ActivePosition, ClosedPosition } from "@/api/watchlist";

/**
 * 今日盈亏纯计算(OverviewPage 汇总卡的数据源)。
 *
 * 基准价规则(2026-07-30 修复):
 * - 今日才开仓的持仓: 基准 = 买入成本(avg_cost ?? entry_price)。
 *   若用昨收, 会把「未持仓的隔夜跳空」算进今日盈亏 ——
 *   例: 昨收 203 隔夜低开 -8.8%, 185.2 买入, 现价 182.75,
 *   按昨收算是 -2030(假亏), 实际今日只亏 (182.75-185.2)×100 = -245。
 * - 早前开仓的持仓: 基准 = 昨收(今日涨跌全部持有)。
 * - 当日平仓同理: 今日开今日平 → (exit - 买入价); 否则 (exit - 昨收)。
 */

/** 单只在仓持仓的今日盈亏; 数据不足返回 null。 */
export function positionTodayPnl(
  pos: Pick<
    ActivePosition,
    "entry_date" | "entry_price" | "avg_cost" | "position_size_shares"
  >,
  cur: number | null,
  prevClose: number | null,
  todayStr: string,
): number | null {
  if (cur == null || pos.position_size_shares == null) return null;
  if (pos.entry_date === todayStr) {
    const basis = pos.avg_cost ?? pos.entry_price;
    if (basis == null) return null;
    return (cur - basis) * pos.position_size_shares;
  }
  if (prevClose == null) return null;
  return (cur - prevClose) * pos.position_size_shares;
}

/** 单条当日平仓记录的「今日那段」盈亏; 数据不足返回 null。 */
export function closedTodayPnl(
  c: ClosedPosition,
  prevClose: number | null,
  todayStr: string,
): number | null {
  const exit = c.close?.actual_exit_price;
  const shares = c.open?.position_size_shares;
  if (exit == null || shares == null) return null;
  if (c.open?.entry_date === todayStr) {
    const basis = c.open?.actual_fill_price ?? c.open?.entry_price;
    if (basis == null) return null;
    return (exit - basis) * shares;
  }
  if (prevClose == null) return null;
  return (exit - prevClose) * shares;
}

/**
 * 今日盈亏合计。
 * 在仓腿: 任一缺数据 → 整体 null(宁可不显示, 不显示错数)。
 * 平仓腿: best-effort, 单条缺数据跳过, 不拖垮在仓部分。
 */
export function calcTodayPnlTotal({
  positions,
  closedToday,
  prices,
  prevClose,
  closedPrevClose,
  todayStr,
}: {
  positions: ActivePosition[];
  closedToday: ClosedPosition[];
  prices: Record<string, number | null> | undefined;
  prevClose: Record<string, number | null> | undefined;
  closedPrevClose: Record<string, number | null> | undefined;
  todayStr: string;
}): number | null {
  let sum = 0;
  for (const pos of positions) {
    const cur = prices?.[pos.ts_code] ?? null;
    const prev = prevClose?.[pos.ts_code] ?? null;
    const pnl = positionTodayPnl(pos, cur, prev, todayStr);
    if (pnl == null) return null;
    sum += pnl;
  }
  for (const c of closedToday) {
    const prev = closedPrevClose?.[c.ts_code] ?? null;
    const pnl = closedTodayPnl(c, prev, todayStr);
    if (pnl == null) continue;
    sum += pnl;
  }
  return positions.length > 0 || closedToday.length > 0 ? sum : null;
}
