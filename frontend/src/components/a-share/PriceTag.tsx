import { cn, directionClass, formatPrice, formatDelta, formatPercent } from "@/lib/utils";

/**
 * <PriceTag> — A 股价格 + 涨跌额/幅, 等宽红绿
 *
 * 数据源: /api/market/prices(当前价) + /api/market/prices/daily(昨收)
 * 涨跌方向 = 当前价 - 昨收
 *
 * ED15 四态:
 *   - loading: 骨架
 *   - error: 「行情异常」
 *   - empty: 停牌无价 → 「停牌」
 *   - 盘外: 当前价 == 昨收(非交易时段两者同值)→ 显示价格但标「盘外·昨收」
 *
 * 红涨绿跌铁律, 克制: 只染数字, 不整行染色。
 */
export interface PriceTagProps {
  /** 当前价(null/undefined = 停牌或未取到) */
  price: number | null | undefined;
  /** 昨收(用于算涨跌方向) */
  prevClose: number | null | undefined;
  /** 加载态 */
  loading?: boolean;
  /** 错误态 */
  error?: boolean;
  /** 尺寸 */
  size?: "sm" | "md" | "lg";
  /** 是否显示涨跌额/幅(默认显示) */
  showChange?: boolean;
  className?: string;
}

export function PriceTag({
  price,
  prevClose,
  loading,
  error,
  size = "md",
  showChange = true,
  className,
}: PriceTagProps) {
  // loading
  if (loading) {
    return (
      <span className={cn("inline-flex flex-col gap-0.5", className)}>
        <span className="num h-5 w-20 animate-pulse rounded bg-bg-card" />
        {showChange && <span className="num h-3 w-16 animate-pulse rounded bg-bg-card" />}
      </span>
    );
  }

  // error
  if (error) {
    return <span className={cn("text-xs text-flat", className)}>行情异常</span>;
  }

  // empty(停牌无价)
  if (price == null || Number.isNaN(price)) {
    return <span className={cn("text-xs text-flat", className)}>停牌</span>;
  }

  // 盘外: 当前价 == 昨收(非交易时段 /api/market/prices 回退日线昨收, 两者同值)
  const isOutside =
    prevClose != null && !Number.isNaN(prevClose) && price === prevClose;

  const delta =
    prevClose != null && !Number.isNaN(prevClose) ? price - prevClose : null;
  const pct =
    delta != null && prevClose != null && prevClose !== 0
      ? (delta / prevClose) * 100
      : null;

  const sizeCls = {
    sm: "text-sm",
    md: "text-base",
    lg: "text-xl",
  }[size];

  return (
    <span className={cn("inline-flex flex-col gap-0.5 leading-tight", className)}>
      <span className="flex items-baseline gap-1.5">
        <span className={cn("num font-medium", sizeCls, "text-text-primary")}>
          {formatPrice(price)}
        </span>
        {isOutside && (
          <span className="text-[10px] text-flat">盘外·昨收</span>
        )}
      </span>
      {showChange && delta != null && (
        <span className={cn("num text-xs", directionClass(delta))}>
          {formatDelta(delta)}{" "}
          <span className="opacity-80">{formatPercent(pct)}</span>
        </span>
      )}
    </span>
  );
}
