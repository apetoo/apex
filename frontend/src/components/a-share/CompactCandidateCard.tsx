import { Bell, TrendingDown, TrendingUp, Target, ShieldAlert, Archive, ArrowUpCircle, Clock, RefreshCw, Sparkles, ChevronDown, Pencil } from "lucide-react";
import { useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag, LatestAnalysisBadge } from "@/components/a-share";
import { getPrices, getDailyPrices } from "@/api/market";
import { useRenewCandidate } from "@/api/mutations";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { Candidate } from "@/api/watchlist";

/** expires_at(YYYY-MM-DD) 距今天剩余天数; 过期返回负数。 */
function daysUntil(expiresAt: string): number | null {
  const d = new Date(expiresAt + "T00:00:00");
  if (Number.isNaN(d.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - today.getTime()) / 86400000);
}

/** 把 expires_meta 拼成一行中文归因, 供 title 悬浮。无 meta 返回 null。 */
function metaTooltip(meta?: Candidate["expires_meta"]): string | null {
  if (!meta) return null;
  if (meta.method === "manual") return `手动设置 ${meta.expires_days} 天`;
  if (meta.method === "fallback") return `数据缺失, 默认 ${meta.expires_days} 天`;
  if (meta.method === "vol_based") {
    const vol = meta.realized_vol != null ? `${(meta.realized_vol * 100).toFixed(2)}%` : "?";
    const dist = meta.distance_pct != null ? `${meta.distance_pct.toFixed(2)}%` : "?";
    const t = meta.t_trading != null ? meta.t_trading.toFixed(1) : "?";
    return `波动率算出 ${meta.expires_days} 天 · σ${vol} · 距离${dist} · t*${t}交易日`;
  }
  return null;
}

/**
 * <CompactCandidateCard> - 竖向紧凑候选卡(网格用)
 *
 * 替代旧行式 CandidateCard。无加仓/减仓按钮(候选未持仓)。
 * 距触发距离配色: 未触发灰 / 接近触发(|pct|<2)黄 / 已触发红字+浅红底。
 * 红涨绿跌铁律: 触发=利好 -> 用 up(红) 表达已触发。
 * 编辑按钮放右上角(icon-only Pencil), 不挤占操作行。
 */
export function CompactCandidateCard({
  candidate,
  onEdit,
  onArchive,
  onPromote,
  onReanalyze,
  onSyncAi,
  syncing,
}: {
  candidate: Candidate;
  /** 人工编辑交易参数(触发价/止损建议/目标建议/过期/strategy 等)。PATCH 部分覆盖。右上角入口。 */
  onEdit?: (c: Candidate) => void;
  onArchive?: (c: Candidate) => void;
  onPromote?: (c: Candidate) => void;
  /** 跳分析页重跑 AI(写新 journal)。过期三选一之一。 */
  onReanalyze?: (c: Candidate) => void;
  /** 同步最近 AI 分析的 entry/stop/target(三字段全覆盖, 不调 AI)。常驻按钮。 */
  onSyncAi?: (c: Candidate) => void;
  syncing?: boolean;
}) {
  const { ts_code, name, trigger_price, trigger_direction, stop_advice, target_advice } =
    candidate;
  const expiresMeta = candidate.expires_meta;
  const renewCount = candidate.renew_count ?? 0;
  const remaining = daysUntil(candidate.expires_at);
  const tooltip = metaTooltip(expiresMeta);
  const isExpiringSoon = remaining != null && remaining <= 2;
  const isExpired = remaining != null && remaining < 0;

  // 续期下拉(过期三选一入口)
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const renewMut = useRenewCandidate();
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);
  const canRenew = renewCount < 3; // 防僵尸: 超 3 次提示该重分析, 不再给续期
  const showMenu = isExpired && (onReanalyze || canRenew || onArchive);

  const hasZone =
    candidate.trigger_low != null && candidate.trigger_high != null;
  const low = candidate.trigger_low ?? null;
  const high = candidate.trigger_high ?? null;

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

  // 带状触发: 价格进入 [low, high] 才触发; 无区间退回单向阈值(below<=price / above>=price)
  const isTriggered =
    currentPrice != null &&
    (hasZone
      ? currentPrice >= (low as number) && currentPrice <= (high as number)
      : trigger_direction === "below"
        ? currentPrice <= trigger_price
        : currentPrice >= trigger_price);

  // 距触发: 带状->到带边的最近距离; 单向->到 trigger_price 的距离
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
      {/* 名称 + 已触发 tag + 编辑(右上角 icon-only) */}
      <div className="flex items-center gap-2">
        <p className="truncate font-medium">{name}</p>
        {isTriggered && (
          <span className="inline-flex items-center gap-0.5 rounded bg-up/10 px-1.5 py-0.5 text-[10px] font-medium text-up">
            <Bell className="h-2.5 w-2.5" /> 已触发
          </span>
        )}
        {onEdit && (
          <button
            type="button"
            onClick={() => onEdit(candidate)}
            className="ml-auto inline-flex items-center justify-center rounded p-1 text-text-secondary hover:bg-bg-base"
            title="编辑触发价/止损建议/目标建议/过期等"
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

      {/* 最近 AI 分析徽标(hover 浮窗 / 点击 Drawer) */}
      <LatestAnalysisBadge tsCode={ts_code} />

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
        {remaining != null && (
          isExpired && showMenu ? (
            <div ref={menuRef} className="relative inline-block">
              <button
                type="button"
                onClick={() => setMenuOpen((v) => !v)}
                className="inline-flex items-center gap-0.5 text-flat hover:text-text-secondary"
                title={tooltip ?? "已过期, 点击处理"}
              >
                <Clock className="h-3 w-3" />
                已过期 <ChevronDown className="h-3 w-3" />
              </button>
              {menuOpen && (
                <div className="absolute left-0 top-5 z-10 w-32 rounded-md border border-border bg-bg-card p-1 shadow-lg">
                  {onReanalyze && (
                    <button
                      type="button"
                      onClick={() => { setMenuOpen(false); onReanalyze(candidate); }}
                      className="flex w-full items-center gap-1 rounded px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-base"
                    >
                      <Sparkles className="h-3 w-3" /> 重分析
                    </button>
                  )}
                  {canRenew ? (
                    <button
                      type="button"
                      disabled={renewMut.isPending}
                      onClick={() => {
                        renewMut.mutate(
                          { ts_code },
                          { onSuccess: () => setMenuOpen(false) },
                        );
                      }}
                      className="flex w-full items-center gap-1 rounded px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-base disabled:opacity-50"
                    >
                      <RefreshCw className={cn("h-3 w-3", renewMut.isPending && "animate-spin")} />
                      {renewMut.isPending ? "续期中" : `续期${renewCount > 0 ? ` (第${renewCount + 1}次)` : ""}`}
                    </button>
                  ) : (
                    <p className="px-2 py-1 text-[10px] text-amber-600">已续 {renewCount} 次, 该重分析</p>
                  )}
                  {onArchive && (
                    <button
                      type="button"
                      onClick={() => { setMenuOpen(false); onArchive(candidate); }}
                      className="flex w-full items-center gap-1 rounded px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-base"
                    >
                      <Archive className="h-3 w-3" /> 归档
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div
              className={cn(
                "flex items-center gap-0.5",
                isExpired ? "text-flat" : isExpiringSoon ? "text-amber-600" : "",
              )}
              title={tooltip ?? undefined}
            >
              <Clock className="h-3 w-3" />
              {isExpired
                ? "已过期"
                : remaining === 0
                  ? "今日到期"
                  : `剩 ${remaining} 天`}
              {expiresMeta?.method === "vol_based" && (
                <span className="ml-1 opacity-60">·波动率算</span>
              )}
              {expiresMeta?.method === "manual" && (
                <span className="ml-1 opacity-60">·手动</span>
              )}
              {renewCount > 0 && (
                <span className="ml-1 opacity-60">·续{renewCount}次</span>
              )}
            </div>
          )
        )}
      </div>

      {/* 操作按钮(可选): 转持仓 / 同步AI / 归档 */}
      {(onPromote || onArchive || onSyncAi) && (
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
          {onSyncAi && (
            <button
              type="button"
              onClick={() => onSyncAi(candidate)}
              disabled={syncing}
              title="用最近一次 AI 分析的 entry/止损/目标覆盖当前值, 顺带重算过期"
              className="inline-flex items-center gap-0.5 rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base disabled:opacity-50"
            >
              <Sparkles className={cn("h-3 w-3", syncing && "animate-pulse")} />
              {syncing ? "同步中" : "同步AI"}
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
