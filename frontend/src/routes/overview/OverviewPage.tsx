import { useQuery } from "@tanstack/react-query";
import { RefreshCw, ArrowUpRight, ArrowDownRight, Target, ShieldAlert, Wallet, Clock } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Button,
} from "@/components/base";
import { PriceTag, MarketIndexBar } from "@/components/a-share";
import { getPrices, getDailyPrices } from "@/api/market";
import { getWatchlist } from "@/api/watchlist";
import { getAccount } from "@/api/account";
import { qk } from "@/api/query-keys";
import { cn, formatPrice, formatPercent } from "@/lib/utils";

/**
 * / 概览首页
 *
 * 顶部两栏: 今日盈亏汇总 + 总资产
 * 下方: 持仓完整列表
 */
export function OverviewPage() {
  const navigate = useNavigate();

  /* ── 数据源 ────────────────────────────────────────── */

  const watchlist = useQuery({
    queryKey: qk.watchlist,
    queryFn: getWatchlist,
  });

  const account = useQuery({
    queryKey: qk.account,
    queryFn: getAccount,
  });

  const positions = watchlist.data?.active_positions ?? [];
  const codes = positions.map((p) => p.ts_code);

  const prices = useQuery({
    queryKey: qk.prices(codes),
    queryFn: () => getPrices(codes),
    enabled: codes.length > 0,
  });

  const daily = useQuery({
    queryKey: qk.dailyPrices(codes),
    queryFn: () => getDailyPrices(codes),
    enabled: codes.length > 0,
  });

  /* ── 今日盈亏汇总 ──────────────────────────────────── */

  const todayPnlTotal: number | null = (() => {
    let sum = 0;
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      if (cur == null || prev == null || pos.position_size_shares == null) return null;
      sum += (cur - prev) * pos.position_size_shares;
    }
    return positions.length > 0 ? sum : null;
  })();

  const acc = account.data;
  const totalCapital = acc?.total_capital ?? null;

  const todayPnlPct: number | null =
    todayPnlTotal != null && totalCapital != null && totalCapital !== 0
      ? (todayPnlTotal / totalCapital) * 100
      : null;

  const pnlIsUp = todayPnlTotal != null && todayPnlTotal >= 0;

  const winningCount = (() => {
    let n = 0;
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      if (cur != null && prev != null && cur - prev > 0) n++;
    }
    return n;
  })();
  const losingCount = positions.length - winningCount;

  const bestToday = (() => {
    let best: { name: string; pct: number } | null = null;
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      if (cur == null || prev == null || prev === 0) continue;
      const pct = ((cur - prev) / prev) * 100;
      if (!best || pct > best.pct) best = { name: pos.name, pct };
    }
    return best;
  })();
  const worstToday = (() => {
    let worst: { name: string; pct: number } | null = null;
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      if (cur == null || prev == null || prev === 0) continue;
      const pct = ((cur - prev) / prev) * 100;
      if (!worst || pct < worst.pct) worst = { name: pos.name, pct };
    }
    return worst;
  })();

  /* ── 全部持仓行情映射 ──────────────────────────────── */

  const pnlMap = new Map<string, { pnl: number | null; pnlPct: number | null }>();
  for (const pos of positions) {
    const cur = prices.data?.[pos.ts_code] ?? null;
    if (cur != null && pos.entry_price != null && pos.position_size_shares != null) {
      const pnl = (cur - pos.entry_price) * pos.position_size_shares;
      const pnlPct = ((cur - pos.entry_price) / pos.entry_price) * 100;
      pnlMap.set(pos.ts_code, { pnl, pnlPct });
    } else {
      pnlMap.set(pos.ts_code, { pnl: null, pnlPct: null });
    }
  }

  const todayPnlMap = new Map<string, { pnl: number | null; pnlPct: number | null }>();
  for (const pos of positions) {
    const cur = prices.data?.[pos.ts_code] ?? null;
    const prev = daily.data?.[pos.ts_code] ?? null;
    if (cur != null && prev != null && prev !== 0 && pos.position_size_shares != null) {
      const pnl = (cur - prev) * pos.position_size_shares;
      const pnlPct = ((cur - prev) / prev) * 100;
      todayPnlMap.set(pos.ts_code, { pnl, pnlPct });
    } else {
      todayPnlMap.set(pos.ts_code, { pnl: null, pnlPct: null });
    }
  }

  const pnlLoading = codes.length > 0 && (prices.isLoading || daily.isLoading);
  const pnlError = codes.length > 0 && (prices.isError || daily.isError);

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="font-serif text-3xl font-semibold tracking-tight">
            概览
          </h1>
          <p className="mt-1 text-sm text-text-secondary">
            今日盘面 · 持仓概览 · 关键信号
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            void prices.refetch();
            void daily.refetch();
            void watchlist.refetch();
            void account.refetch();
          }}
        >
          <RefreshCw className="mr-1 h-3.5 w-3.5" />
          刷新
        </Button>
      </div>

      {/* 市场温度(满宽) */}
      <MarketIndexBar />

      {/* 两栏: 今日盈亏 + 总资产 */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* 今日盈亏汇总 */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-normal text-text-secondary">
              <Clock className="mr-1 inline h-3.5 w-3.5" />
              今日盈亏
            </CardTitle>
            {pnlLoading && (
              <span className="text-[10px] text-text-secondary">更新中…</span>
            )}
          </CardHeader>
          <CardContent>
            {positions.length === 0 ? (
              <p className="text-sm text-flat">暂无持仓</p>
            ) : pnlError ? (
              <p className="text-xs text-flat">加载失败</p>
            ) : todayPnlTotal != null ? (
              <div>
                <p
                  className={cn(
                    "num text-2xl font-semibold",
                    pnlIsUp ? "text-up" : "text-down",
                  )}
                >
                  {pnlIsUp ? "+" : ""}
                  {todayPnlTotal.toFixed(2)} 元
                </p>
                {todayPnlPct != null && (
                  <p
                    className={cn(
                      "num mt-1 text-sm",
                      pnlIsUp ? "text-up" : "text-down",
                    )}
                  >
                    {pnlIsUp ? "+" : ""}
                    {todayPnlPct.toFixed(2)}%
                  </p>
                )}
                <div className="mt-3 flex items-center gap-3 text-xs text-text-secondary">
                  <span>盈利 {winningCount} 只</span>
                  <span>亏损 {losingCount} 只</span>
                </div>
                {(bestToday || worstToday) && (
                  <div className="mt-1.5 space-y-0.5 text-[11px] text-text-secondary">
                    {bestToday && (
                      <span className="block">
                        最佳 <span className="text-up">{bestToday.name} {formatPercent(bestToday.pct)}</span>
                      </span>
                    )}
                    {worstToday && (
                      <span className="block">
                        最差 <span className="text-down">{worstToday.name} {formatPercent(worstToday.pct)}</span>
                      </span>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <span className="num inline-block h-7 w-24 animate-pulse rounded bg-bg-card" />
            )}
          </CardContent>
        </Card>

        {/* 总资产 */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-normal text-text-secondary">
              <Wallet className="mr-1 inline h-3.5 w-3.5" />
              总资产
            </CardTitle>
          </CardHeader>
          <CardContent>
            {account.isLoading ? (
              <span className="num inline-block h-7 w-24 animate-pulse rounded bg-bg-card" />
            ) : account.isError ? (
              <p className="text-xs text-flat">加载失败</p>
            ) : totalCapital != null ? (
              <div>
                <p className="num text-2xl font-semibold text-text-primary">
                  {totalCapital.toLocaleString("zh-CN", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </p>
                <p className="mt-1 text-xs text-text-secondary">
                  总资金 · 每笔风险 {acc?.risk_per_trade_pct ?? "—"}%
                </p>
              </div>
            ) : (
              <p className="num text-2xl font-semibold text-flat">—</p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* 持仓完整列表 */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle>持仓概览</CardTitle>
            <span className="num text-xs text-text-secondary">
              {positions.length} 只
            </span>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate("/watchlist")}
          >
            管理持仓 →
          </Button>
        </CardHeader>
        <CardContent className="pt-0">
          {positions.length === 0 ? (
            <p className="py-6 text-center text-sm text-flat">
              暂无持仓 · 前往
              <button
                className="mx-1 text-up underline underline-offset-2 hover:no-underline"
                onClick={() => navigate("/analyze")}
              >
                分析页
              </button>
              添加
            </p>
          ) : (
            positions.map((pos) => {
              const cur = prices.data?.[pos.ts_code] ?? null;
              const prev = daily.data?.[pos.ts_code] ?? null;
              const loading = prices.isLoading || daily.isLoading;
              const error = prices.isError || daily.isError;
              const delta = cur != null && prev != null ? cur - prev : null;
              const pnlInfo = pnlMap.get(pos.ts_code);
              const todayPnlInfo = todayPnlMap.get(pos.ts_code);

              return (
                <div
                  key={pos.ts_code}
                  className="flex items-center justify-between gap-4 border-b border-border py-4 last:border-0"
                >
                  {/* 左侧 */}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate font-medium">{pos.name}</p>
                      {pos.strategy && (
                        <span className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-text-secondary">
                          {pos.strategy}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-xs text-text-secondary">
                      {pos.ts_code}
                    </p>
                    {(pos.stop_loss != null || pos.target != null) && (
                    <div className="mt-1.5 flex items-center gap-3 text-[11px] text-text-secondary">
                      <span className="flex items-center gap-0.5">
                        <ShieldAlert className="h-3 w-3 text-down" />
                        止损 {formatPrice(pos.stop_loss)}
                      </span>
                      <span className="flex items-center gap-0.5">
                        <Target className="h-3 w-3 text-up" />
                        目标 {formatPrice(pos.target)}
                      </span>
                    </div>
                    )}
                  </div>

                  {/* 右侧: 价格 + 盈亏 */}
                  <div className="text-right">
                    <PriceTag
                      price={cur}
                      prevClose={prev}
                      loading={loading}
                      error={error}
                      size="md"
                      showChange
                    />
                    {pnlInfo?.pnl != null && (
                      <div
                        className={cn(
                          "mt-1 flex items-center justify-end gap-0.5 num text-xs",
                          pnlInfo.pnl >= 0 ? "text-up" : "text-down",
                        )}
                      >
                        {pnlInfo.pnl >= 0 ? (
                          <ArrowUpRight className="h-3 w-3" />
                        ) : (
                          <ArrowDownRight className="h-3 w-3" />
                        )}
                        <span>
                          {pnlInfo.pnl >= 0 ? "+" : ""}
                          {pnlInfo.pnl.toFixed(0)} 元
                        </span>
                        {pnlInfo.pnlPct != null && (
                          <span className="ml-1 opacity-80">
                            ({pnlInfo.pnlPct >= 0 ? "+" : ""}
                            {pnlInfo.pnlPct.toFixed(2)}%)
                          </span>
                        )}
                      </div>
                    )}
                    {todayPnlInfo?.pnl != null && (
                      <div
                        className={cn(
                          "mt-0.5 flex items-center justify-end gap-0.5 num text-[10px]",
                          todayPnlInfo.pnl >= 0 ? "text-up" : "text-down",
                        )}
                      >
                        <span>今日</span>
                        <span>
                          {todayPnlInfo.pnl >= 0 ? "+" : ""}
                          {todayPnlInfo.pnl.toFixed(0)} 元
                        </span>
                        {todayPnlInfo.pnlPct != null && (
                          <span className="opacity-80">
                            ({todayPnlInfo.pnlPct >= 0 ? "+" : ""}
                            {todayPnlInfo.pnlPct.toFixed(2)}%)
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </CardContent>
      </Card>
    </div>
  );
}
