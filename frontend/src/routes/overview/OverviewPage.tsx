import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Wallet, Clock } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Button,
} from "@/components/base";
import { MarketIndexBar } from "@/components/a-share";
import { getPrices, getPrevClosePrices } from "@/api/market";
import { getWatchlist, getClosedPositions } from "@/api/watchlist";
import { getAccount } from "@/api/account";
import { qk } from "@/api/query-keys";
import { cn, formatPercent } from "@/lib/utils";
import { CompactPositionCard } from "@/components/a-share/CompactPositionCard";

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

  // 昨收: 用 prev-close(排除今天), 不能用 /prices/daily —— 盘后 tushare 发了当日
  // 个股 daily 后, daily 会返回今日收盘, 导致今日盈亏 cur==prev 恒为 0。
  const daily = useQuery({
    queryKey: qk.prevClose(codes),
    queryFn: () => getPrevClosePrices(codes),
    enabled: codes.length > 0,
  });

  /* ── 当日平仓：今日盈亏要计入「今日那段」(exit_price - 昨收) × 股数 ─────── */
  // 平仓后仓位从 active_positions 移除, 否则今日盈亏会漏掉这笔今天的涨跌。
  // CN 时区今日字符串, 与后端 close.exit_date (date.today().isoformat()) 对齐。
  const todayStr = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date()).replace(/\//g, "-");

  const closedRecent = useQuery({
    queryKey: qk.closedRecent,
    queryFn: () => getClosedPositions({ since_days: 3 }),
  });

  const closedToday = (closedRecent.data ?? []).filter(
    (c) => c.close?.exit_date === todayStr,
  );
  const closedCodes = closedToday.map((c) => c.ts_code);

  const closedPrevClose = useQuery({
    queryKey: qk.prevClose(closedCodes),
    queryFn: () => getPrevClosePrices(closedCodes),
    enabled: closedCodes.length > 0,
  });

  /* ── 今日盈亏汇总 ──────────────────────────────────── */

  const todayPnlTotal: number | null = (() => {
    let sum = 0;
    // 在仓: (现价 - 昨收) × 股数
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      if (cur == null || prev == null || pos.position_size_shares == null) return null;
      sum += (cur - prev) * pos.position_size_shares;
    }
    // 当日平仓: (exit_price - 昨收) × 股数（best-effort, 取不到昨收则跳过这条, 不拖垮整指标）
    for (const c of closedToday) {
      const exitPrice = c.close?.actual_exit_price;
      const prev = closedPrevClose.data?.[c.ts_code] ?? null;
      const shares = c.open?.position_size_shares ?? null;
      if (exitPrice == null || prev == null || shares == null) continue;
      sum += (exitPrice - prev) * shares;
    }
    return positions.length > 0 || closedToday.length > 0 ? sum : null;
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
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {positions.map((pos) => (
                <CompactPositionCard key={pos.ts_code} position={pos} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
