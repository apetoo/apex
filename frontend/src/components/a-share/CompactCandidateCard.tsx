import { Bell, TrendingDown, TrendingUp, Target, ShieldAlert, Archive, ArrowUpCircle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { Candidate } from "@/api/watchlist";

/**
 * <CompactCandidateCard> — 竖向紧凑候选卡(网格用)
 *
 * 替代旧行式 CandidateCard。无加仓/减仓按钮(候选未持仓)。
 * 距触发距离配色: 未触发灰 / 接近触发(|pct|<2)黄 / 已触发红字+浅红底。
 * 红涨绿跌铁律: 触发=利好 → 用 up(红) 表达已触发。
 */
export function CompactCandidateCard({
  candidate,
  onArchive,
  onPromote,
}: {
  candidate: Candidate;
  onArchive?: (c: Candidate) => void;
  onPromote?: (c: Candidate) => void;
}) {
  const { ts_code, name, trigger_price, trigger_direction, stop_advice, target_advice } =
    candidate;
  const hasZone =
    candidate.trigger_low != null && candidate.trigger_high != null;
  const low = candidate.trigger_low ?? null;
  const high = candidate.trigger_high ?? null;

  const prices = useQuery({
    queryKey: qk.prices([ts_code]),
    queryFn: () => getPrices([ts_code]),
  });

  const currentPrice = prices.data?.[ts_code] ?? null;
  const loading = prices.isLoading;
  const error = prices.isError;

  // 带状触发: 价格进入 [low, high] 才触发; 无区间退回单向阈值(below<=price / above>=price)
  const isTriggered =
    currentPrice != null &&
    (hasZone
      ? currentPrice >= (low as number) && currentPrice <= (high as number)
      : trigger_direction === "below"
        ? currentPrice <= trigger_price
        : currentPrice >= trigger_price);

  // 距触发: 带状→到带边的最近距离; 单向→到 trigger_price 的距离
  const anchor =
    currentPrice != null
      ? hasZone
        ? trigger_direction === "below"
          ? Math.max((low as number) - currentPrice, 0) // 等回踩到下沿
          : Math.max(currentPrice - (high as number), 0) // 已突破上沿外, 否则 0(带内)
          : currentPrice - trigger_price
      : null;
  const distance = anchor;
  const distancePct =
    currentPrice != null && trigger_price !== 0
      ? ((distance ?? 0) / trigger_price) * 100
      : null;
  const isNear =
    !isTriggered && distancePct != null && Math.abs(distancePct) < 2;

  const TrendingIcon = trigger_direction === "below" ? TrendingDown : TrendingUp;

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-bg-card p-3",
        isTriggered && "bg-up/5",
      )}
    >
      {/* 名称 + 已触发 tag */}
      <div className="flex items-center gap-2">
        <p className="truncate font-medium">{name}</p>
        {isTriggered && (
          <span className="inline-flex items-center gap-0.5 rounded bg-up/10 px-1.5 py-0.5 text-[10px] font-medium text-up">
            <Bell className="h-2.5 w-2.5" /> 已触发
          </span>
        )}
      </div>
      <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>

      {/* 当前价(黑色, 候选不展示当日涨跌) */}
      <div className="mt-2">
        <PriceTag
          price={currentPrice}
          prevClose={null}
          loading={loading}
          error={error}
          size="lg"
          showChange={false}
        />
      </div>

      {/* 距触发(已触发时整行不渲染, 名称旁的 Bell tag 已是充分指示, 避免文本重复) */}
      {!isTriggered && distance != null && distancePct != null ? (
        <p
          className={cn(
            "num mt-1 text-xs",
            isNear ? "text-amber-600" : "text-text-secondary",
          )}
        >
          距触发 {distance >= 0 ? "+" : ""}
          {distance.toFixed(2)} ({distancePct >= 0 ? "+" : ""}
          {distancePct.toFixed(2)}%)
        </p>
      ) : null}

      <div className="my-2 border-t border-border" />

      {/* 参数行: 触发价+方向 / 建议止损 / 目标 */}
      <div className="space-y-1 text-[11px] text-text-secondary">
        <div className="num flex items-center gap-0.5">
          <TrendingIcon className="h-3 w-3" />
          {hasZone
            ? `触发带 ${formatPrice(low as number)}–${formatPrice(high as number)}`
            : `触发 ${formatPrice(trigger_price)}(${
                trigger_direction === "below" ? "下方" : "上方"
              })`}
        </div>
        {(stop_advice > 0 || target_advice > 0) && (
          <div className="num flex items-center gap-3">
            {stop_advice > 0 && (
              <span className="flex items-center gap-0.5">
                <ShieldAlert className="h-3 w-3 text-down" />
                {formatPrice(stop_advice)}
              </span>
            )}
            {target_advice > 0 && (
              <span className="flex items-center gap-0.5">
                <Target className="h-3 w-3 text-up" />
                {formatPrice(target_advice)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* 操作按钮(可选): 转持仓 / 归档 */}
      {(onPromote || onArchive) && (
        <div className="mt-2 flex items-center gap-1.5">
          {onPromote && (
            <button
              type="button"
              onClick={() => onPromote(candidate)}
              className="inline-flex items-center gap-0.5 rounded border border-up/30 px-2 py-0.5 text-[11px] text-up hover:bg-up/5"
            >
              <ArrowUpCircle className="h-3 w-3" />
              转持仓
            </button>
          )}
          {onArchive && (
            <button
              type="button"
              onClick={() => onArchive(candidate)}
              className="ml-auto inline-flex items-center gap-0.5 rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
            >
              <Archive className="h-3 w-3" />
              归档
            </button>
          )}
        </div>
      )}
    </div>
  );
}
