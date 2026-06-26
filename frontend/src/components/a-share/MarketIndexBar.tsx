import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Activity, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Button } from "@/components/base";
import { getIndexDaily, type IndexDaily } from "@/api/market";
import { cn, directionClass, formatDelta, formatPercent } from "@/lib/utils";

/**
 * <MarketIndexBar> — 市场温度(沪指/深指)横向条
 *
 * 借鉴 21st.dev Stats Card 结构 + 多个 stat 横向排列。
 * 配色严格 A 股红涨绿跌。
 *
 * 数据源(ED13): /api/market/index-daily?code=...
 *   单根来源, 返回 close + vol + pct_chg(足够显示全部)
 *   替代之前 prices+daily 双拉(避免深证成指等指数新浪接口失败的问题)
 *
 * 盘外(ED15): 涨跌幅=0 且 latest 与 prev 一致 → 标「盘外·昨收」
 */
const INDEX_CODES = ["000001.SH", "399001.SZ"] as const;
const INDEX_NAMES: Record<string, string> = {
  "000001.SH": "上证指数",
  "399001.SZ": "深证成指",
};

export function MarketIndexBar() {
  // 单根数据源(ED13)
  const index000001 = useQuery({
    queryKey: ["index-daily", "000001.SH"],
    queryFn: () => getIndexDaily("000001.SH"),
  });
  const index399001 = useQuery({
    queryKey: ["index-daily", "399001.SZ"],
    queryFn: () => getIndexDaily("399001.SZ"),
  });
  const indexMap: Record<string, IndexDaily | undefined> = {
    "000001.SH": index000001.data,
    "399001.SZ": index399001.data,
  };

  const refetch = () => {
    void index000001.refetch();
    void index399001.refetch();
  };

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
        {index000001.isError || index399001.isError ? (
          <p className="text-xs text-flat">行情异常</p>
        ) : (
          <div className="grid grid-cols-2 gap-x-6 gap-y-3">
            {INDEX_CODES.map((code) => {
              const idx = indexMap[code];
              const loading = !idx;
              const bars = idx?.bars ?? [];
              const latest = bars[bars.length - 1];
              const prev = bars[bars.length - 2];
              const price = latest?.close ?? null;
              const prevClose = prev?.close ?? null;
              const pct = latest?.pct_chg ?? null;
              const delta =
                price != null && prevClose != null ? price - prevClose : null;
              // 盘外: 涨跌幅 = 0 且价格 = 昨收(ED15)
              const isOutside = pct === 0 && price === prevClose;
              const isUp = delta != null && delta > 0;
              const TrendIcon = isUp ? TrendingUp : TrendingDown;
              const vol = latest?.vol;
              const volDisplay =
                vol == null
                  ? null
                  : vol / 1e8 >= 1
                    ? `${(vol / 1e8).toFixed(1)} 亿手`
                    : `${Math.round(vol / 1e4)} 万手`;
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
                  {isOutside && (
                    <p className="text-[10px] text-flat">盘外·昨收</p>
                  )}
                  {volDisplay && (
                    <p className="num mt-0.5 text-[10px] text-flat">
                      量 {volDisplay}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <p className="mt-3 text-[10px] text-flat">
          ED4 手动刷新 · 来自 /api/market/index-daily(ED13)
        </p>
      </CardContent>
    </Card>
  );
}
