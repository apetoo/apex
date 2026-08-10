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
import { getAccount, getAccountSummary } from "@/api/account";
import { qk } from "@/api/query-keys";
import { cn, formatPercent } from "@/lib/utils";
import { CompactPositionCard } from "@/components/a-share/CompactPositionCard";
import { PushStatusCard } from "@/routes/overview/PushStatusCard";
import { calcTodayPnlTotal, positionTodayPnl } from "@/routes/overview/todayPnl";

/**
 * / 概览首页
 *
 * 顶部两栏: 今日盈亏汇总 + 总资产
 * 下方: 持仓完整列表
 */

const fmtMoney = (n: number | null | undefined) =>
  n == null || Number.isNaN(n)
    ? "—"
    : n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmtSigned = (n: number | null | undefined) =>
  n == null || Number.isNaN(n)
    ? "—"
    : `${n >= 0 ? "+" : ""}${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function SummaryRow({ label, value, tone }: { label: string; value: string; tone?: number }) {
  const toneClass =
    tone == null || tone === 0
      ? "text-text-primary"
      : tone > 0
        ? "text-up"
        : "text-down";
  return (
    <div className="flex justify-between text-text-secondary">
      <span>{label}</span>
      <span className={cn("num", toneClass)}>{value}</span>
    </div>
  );
}

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

  const summary = useQuery({
    queryKey: qk.accountSummary,
    queryFn: getAccountSummary,
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

  /* ── 当日平仓：今日盈亏要计入「今日那段」(exit - 基准) × 股数 ─────────── */
  // 平仓后仓位从 active_positions 移除, 否则今日盈亏会漏掉这笔今天的涨跌。
  // 基准 = 昨日开仓→昨收; 今日开今日平→买入价(见 todayPnl.ts)。
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

  const todayPnlTotal = calcTodayPnlTotal({
    positions,
    closedToday,
    prices: prices.data,
    prevClose: daily.data,
    closedPrevClose: closedPrevClose.data,
    todayStr,
  });

  const acc = account.data;
  const totalCapital = acc?.total_capital ?? null;

  const todayPnlPct: number | null =
    todayPnlTotal != null && totalCapital != null && totalCapital !== 0
      ? (todayPnlTotal / totalCapital) * 100
      : null;

  const pnlIsUp = todayPnlTotal != null && todayPnlTotal >= 0;

  // 盈亏计数与合计同口径(positionTodayPnl 基准): 今日开仓按买入价, 否则按昨收。
  const winningCount = (() => {
    let n = 0;
    for (const pos of positions) {
      const cur = prices.data?.[pos.ts_code] ?? null;
      const prev = daily.data?.[pos.ts_code] ?? null;
      const pnl = positionTodayPnl(pos, cur, prev, todayStr);
      if (pnl != null && pnl > 0) n++;
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
            void summary.refetch();
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

        {/* 总资产: 本金 + 累计已实现 + 浮盈 */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-normal text-text-secondary">
              <Wallet className="mr-1 inline h-3.5 w-3.5" />
              总资产
            </CardTitle>
          </CardHeader>
          <CardContent>
            {summary.isLoading ? (
              <span className="num inline-block h-7 w-24 animate-pulse rounded bg-bg-card" />
            ) : summary.isError || !summary.data ? (
              <p className="text-xs text-flat">加载失败</p>
            ) : (
              <div>
                <p className="num text-2xl font-semibold text-text-primary">
                  {summary.data.total_assets.toLocaleString("zh-CN", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </p>
                {summary.data.total_return_pct != null && (
                  <p
                    className={cn(
                      "num mt-1 text-sm",
                      summary.data.total_return_pct >= 0 ? "text-up" : "text-down",
                    )}
                  >
                    {formatPercent(summary.data.total_return_pct)} 总收益率
                  </p>
                )}
                <div className="mt-3 space-y-0.5 text-[11px]">
                  <SummaryRow label="持仓市值" value={fmtMoney(summary.data.market_value)} />
                  <SummaryRow label="持仓成本" value={fmtMoney(summary.data.cost_basis)} />
                  <SummaryRow
                    label="浮盈"
                    value={`${fmtSigned(summary.data.unrealized_pnl)}${summary.data.unrealized_pnl_pct != null ? `  ${formatPercent(summary.data.unrealized_pnl_pct)}` : ""}`}
                    tone={summary.data.unrealized_pnl}
                  />
                  <SummaryRow
                    label="已实现"
                    value={fmtSigned(summary.data.realized_pnl_total)}
                    tone={summary.data.realized_pnl_total}
                  />
                  <SummaryRow label="本金" value={fmtMoney(summary.data.total_capital)} />
                </div>
                {summary.data.missing_price_count > 0 && (
                  <p className="mt-2 text-[10px] text-text-secondary">
                    {summary.data.missing_price_count} 只无现价, 未计入市值
                  </p>
                )}
              </div>
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

      {/* 持仓推送：状态 + 手动全量触发 */}
      <PushStatusCard />
    </div>
  );
}
