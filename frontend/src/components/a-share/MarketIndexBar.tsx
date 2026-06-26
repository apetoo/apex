import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Activity, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Button } from "@/components/base";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, directionClass, formatDelta, formatPercent } from "@/lib/utils";

/**
 * <MarketIndexBar> — 市场温度(沪指/深指)横向条
 *
 * 借鉴 21st.dev Stats Card 结构 + 多个 stat 横向排列。
 * 配色严格 A 股红涨绿跌。
 *
 * 数据源: /api/market/prices?codes=000001.SH,399001.SZ + /daily
 * 成交量(ED13): /api/market/index-daily?code=000001.SH(P 待加端点)
 *
 * 盘外(ED15): 当前价 == 昨收时标「盘外·昨收」, 涨跌额/幅 = 0 显示灰。
 */
const INDEX_CODES = ["000001.SH", "399001.SZ"] as const;
const INDEX_NAMES: Record<string, string> = {
  "000001.SH": "上证指数",
  "399001.SZ": "深证成指",
};

export function MarketIndexBar() {
  const prices = useQuery({
    queryKey: qk.prices([...INDEX_CODES]),
    queryFn: () => getPrices([...INDEX_CODES]),
  });
  const daily = useQuery({
    queryKey: qk.dailyPrices([...INDEX_CODES]),
    queryFn: () => getDailyPrices([...INDEX_CODES]),
  });

  const loading = prices.isLoading || daily.isLoading;
  const error = prices.isError || daily.isError;

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
          onClick={() => {
            void prices.refetch();
            void daily.refetch();
          }}
          aria-label="刷新指数"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </CardHeader>
      <CardContent className="pt-0">
        {error ? (
          <p className="text-xs text-flat">行情异常</p>
        ) : (
          <div className="grid grid-cols-2 gap-x-6 gap-y-3">
            {INDEX_CODES.map((code) => {
              const price = prices.data?.[code] ?? null;
              const prev = daily.data?.[code] ?? null;
              const delta =
                price != null && prev != null ? price - prev : null;
              const pct =
                delta != null && prev != null && prev !== 0
                  ? (delta / prev) * 100
                  : null;
              const isUp = delta != null && delta > 0;
              const TrendIcon = isUp ? TrendingUp : TrendingDown;
              return (
                <div key={code} className="flex flex-col">
                  <span className="text-xs text-text-secondary">
                    {INDEX_NAMES[code]}
                  </span>
                  {loading || price == null ? (
                    <span className="num mt-1 h-6 w-20 animate-pulse rounded bg-bg-base" />
                  ) : (
                    <span
                      className={cn(
                        "num mt-1 text-xl font-semibold",
                        directionClass(delta),
                      )}
                    >
                      {price.toFixed(2)}
                    </span>
                  )}
                  {delta != null && pct != null && (
                    <div
                      className={cn(
                        "num mt-0.5 flex items-center gap-1 text-xs",
                        directionClass(delta),
                      )}
                    >
                      <TrendIcon className="h-3 w-3" />
                      <span>{formatDelta(delta)}</span>
                      <span className="opacity-80">{formatPercent(pct)}</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <p className="mt-3 text-[10px] text-flat">
          ED4 手动刷新 · 成交量走 /api/market/index-daily(ED13 待加)
        </p>
      </CardContent>
    </Card>
  );
}
