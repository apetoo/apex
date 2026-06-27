import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Activity, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Button } from "@/components/base";
import { getIndexDailyBatch, type IndexDaily } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, directionClass, formatPercent, amountKToYi, formatVolume } from "@/lib/utils";

/**
 * <MarketIndexBar> — 市场温度(6 大指数 + 两市合计成交额)
 *
 * 6 大指数: 上证/深成/创业板/沪深300/中证500/科创50
 * 两市合计成交额 = 000001.SH(上证综指) + 399106.SZ(深证综指) 的 amount
 *   注: 深市用综指 399106(全覆盖), 非成指 399001(仅 500 只, 会偏低)
 *   amount 单位千元, ÷1e4 转亿元后相加
 * 放量红/缩量绿(A 股红涨绿跌惯例)
 * 盘外: 最新 bar 的 trade_date ≠ 今天 → 「盘外·<周几>收盘(<MMDD>)」
 *
 * 数据源: /api/market/index-daily/batch(ED13, 一次拉 7 只)
 */
const DISPLAY_INDICES = [
  { code: "000001.SH", name: "上证指数" },
  { code: "399001.SZ", name: "深证成指" },
  { code: "399006.SZ", name: "创业板指" },
  { code: "000300.SH", name: "沪深300" },
  { code: "000905.SH", name: "中证500" },
  { code: "000688.SH", name: "科创50" },
] as const;

// 两市合计成交额的工具指数
const SH_COMP = "000001.SH"; // 沪市成交(上证综指, 已在 DISPLAY_INDICES 中复用)
const SZ_COMP = "399106.SZ"; // 深市成交(深证综指, 不展示)

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 本地今天的 YYYYMMDD(单用户工具, 本地时区够用) */
function todayStr(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}${m}${day}`;
}

/** "20260626" → "周五" */
function weekdayOf(yyyymmdd: string): string {
  const y = Number(yyyymmdd.slice(0, 4));
  const m = Number(yyyymmdd.slice(4, 6)) - 1;
  const d = Number(yyyymmdd.slice(6, 8));
  return WEEKDAYS[new Date(y, m, d).getDay()];
}

/** "20260626" → "0626" */
function mdOf(yyyymmdd: string): string {
  return yyyymmdd.slice(4, 8);
}

export function MarketIndexBar() {
  // 7 只: 6 展示 + 399106.SZ(深证综指, 仅取 amount)
  const allCodes = [...DISPLAY_INDICES.map((i) => i.code), SZ_COMP];
  const query = useQuery({
    queryKey: qk.indexDailyBatch(allCodes),
    queryFn: () => getIndexDailyBatch(allCodes),
  });
  const data: Record<string, IndexDaily | undefined> = query.data ?? {};

  const refetch = () => void query.refetch();

  // ── 两市合计成交额(亿元) ──
  const shBars = data[SH_COMP]?.bars ?? [];
  const szBars = data[SZ_COMP]?.bars ?? [];
  const shLatest = shBars[shBars.length - 1];
  const shPrevBar = shBars[shBars.length - 2];
  const szLatest = szBars[szBars.length - 1];
  const szPrevBar = szBars[szBars.length - 2];
  const shToday = amountKToYi(shLatest?.amount);
  const shPrev = amountKToYi(shPrevBar?.amount);
  const szToday = amountKToYi(szLatest?.amount);
  const szPrev = amountKToYi(szPrevBar?.amount);
  const todayTotal =
    shToday != null && szToday != null ? shToday + szToday : null;
  const prevTotal =
    shPrev != null && szPrev != null ? shPrev + szPrev : null;
  const diff =
    todayTotal != null && prevTotal != null ? todayTotal - prevTotal : null;
  const diffPct =
    diff != null && prevTotal != null && prevTotal !== 0
      ? (diff / prevTotal) * 100
      : null;
  const isVolume = diff != null && diff > 0; // 放量
  const isShrink = diff != null && diff < 0; // 缩量
  const volLabel = isVolume ? "放量" : isShrink ? "缩量" : "持平";

  // ── 盘外判定(最新 bar 日期 ≠ 今天) ──
  const latestDate = shLatest?.trade_date;
  const prevDate = shPrevBar?.trade_date;
  const isOutside = latestDate != null && latestDate !== todayStr();

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-text-secondary" />
          <CardTitle className="text-sm font-normal text-text-secondary">
            市场温度
          </CardTitle>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={refetch}
          aria-label="刷新指数"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </CardHeader>
      <CardContent className="pt-0">
        {query.isError ? (
          <p className="text-xs text-flat">行情异常</p>
        ) : (
          <>
            {/* 两市合计成交额 */}
            <div className="mb-3 border-b border-border/60 pb-3">
              <span className="text-xs text-text-secondary">两市合计成交</span>
              {todayTotal == null ? (
                <span className="num mt-1 block h-7 w-32 animate-pulse rounded bg-bg-base" />
              ) : (
                <div className="mt-1 flex items-baseline gap-2">
                  <span className="num text-xl font-semibold text-text-primary">
                    {formatVolume(todayTotal)}
                  </span>
                  {diff != null && (
                    <span
                      className={cn(
                        "num flex items-center gap-1 text-xs",
                        directionClass(diff),
                      )}
                    >
                      {isVolume ? (
                        <TrendingUp className="h-3 w-3" />
                      ) : isShrink ? (
                        <TrendingDown className="h-3 w-3" />
                      ) : null}
                      <span>{volLabel}</span>
                      <span>
                        {diff > 0 ? "+" : ""}
                        {diff.toFixed(0)} 亿
                      </span>
                      {diffPct != null && (
                        <span className="opacity-80">
                          {formatPercent(diffPct)}
                        </span>
                      )}
                    </span>
                  )}
                </div>
              )}
              {prevTotal != null && prevDate && (
                <p className="num mt-0.5 text-[10px] text-flat">
                  昨日 {formatVolume(prevTotal)} · {mdOf(prevDate)}
                </p>
              )}
            </div>

            {/* 6 大指数网格 */}
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
              {DISPLAY_INDICES.map((idx) => {
                const bars = data[idx.code]?.bars ?? [];
                const latest = bars[bars.length - 1];
                const price = latest?.close ?? null;
                const pct = latest?.pct_chg ?? null;
                const isUp = pct != null && pct > 0;
                const TrendIcon = isUp ? TrendingUp : TrendingDown;
                return (
                  <div key={idx.code} className="flex flex-col">
                    <span className="text-xs text-text-secondary">
                      {idx.name}
                    </span>
                    {price == null ? (
                      <span className="num mt-1 h-6 w-20 animate-pulse rounded bg-bg-base" />
                    ) : (
                      <span className="num mt-1 text-lg font-semibold text-text-primary">
                        {price.toFixed(2)}
                      </span>
                    )}
                    {pct != null && (
                      <div
                        className={cn(
                          "num mt-0.5 flex items-center gap-1 text-xs",
                          directionClass(pct),
                        )}
                      >
                        <TrendIcon className="h-3 w-3" />
                        <span>{formatPercent(pct)}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {isOutside && latestDate && (
              <p className="mt-3 text-[10px] text-flat">
                盘外·{weekdayOf(latestDate)}收盘({mdOf(latestDate)})
              </p>
            )}
          </>
        )}
        <p className="mt-3 text-[10px] text-flat">
          ED4 手动刷新 · 来自 /api/market/index-daily/batch(ED13)
        </p>
      </CardContent>
    </Card>
  );
}
