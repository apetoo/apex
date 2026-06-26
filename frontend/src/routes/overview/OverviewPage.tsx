import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, RefreshCw } from "lucide-react";
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
import { qk } from "@/api/query-keys";
import { cn, directionClass, formatPrice, formatPercent } from "@/lib/utils";

/**
 * / 概览首页(PR3 完整实现, PR1a 联调版)
 *
 * 借鉴 21st.dev Stats Card + Portfolio Card 结构, 翻成 A 股红涨绿跌。
 * 顶部一行: 联调单票(主, lg) + 市场温度(中, 沪指/深指) + 总资产占位
 * 下方: 持仓概览卡片(借鉴 21st.dev Portfolio Card divide-y 列表)
 */
export function OverviewPage() {
  const navigate = useNavigate();

  // 联调用: 平安银行 000001.SZ(PR3 改为持仓列表批量)
  const codes = ["000001.SZ"];

  const prices = useQuery({
    queryKey: qk.prices(codes),
    queryFn: () => getPrices(codes),
    refetchInterval: false, // ED4 全手动刷新
  });

  const daily = useQuery({
    queryKey: qk.dailyPrices(codes),
    queryFn: () => getDailyPrices(codes),
  });

  const price = prices.data?.["000001.SZ"] ?? null;
  const prevClose = daily.data?.["000001.SZ"] ?? null;
  const loading = prices.isLoading || daily.isLoading;
  const error = prices.isError || daily.isError;

  const delta =
    price != null && prevClose != null ? price - prevClose : null;
  const isUp = delta != null && delta > 0;
  const TrendIcon = isUp ? TrendingUp : TrendingDown;

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">
          概览
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          今日盘面 · 持仓概览 · 关键信号
        </p>
      </div>

      {/* 顶部三栏: 联调主票 + 市场温度 + 总资产占位 */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-normal text-text-secondary">
              平安银行 000001.SZ
            </CardTitle>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => {
                void prices.refetch();
                void daily.refetch();
              }}
              aria-label="刷新行情"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </CardHeader>
          <CardContent>
            <PriceTag
              price={price}
              prevClose={prevClose}
              loading={loading}
              error={error}
              size="lg"
            />
          </CardContent>
        </Card>

        <MarketIndexBar />

        <Card className="opacity-60">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-normal text-text-secondary">
              总资产
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="num text-2xl font-semibold text-flat">—</p>
            <p className="mt-1 text-xs text-flat">PR3 接入 /api/account</p>
          </CardContent>
        </Card>
      </div>

      {/* 持仓概览: 借鉴 21st.dev Portfolio Card, divide-y 列表 */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>持仓概览</CardTitle>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate("/watchlist")}
          >
            管理持仓 →
          </Button>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="flex items-center justify-between border-b border-border py-4 last:border-0">
            <div className="flex items-center gap-4">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-bg-base">
                <span className="text-xs font-medium text-text-secondary">
                  PA
                </span>
              </div>
              <div>
                <p className="font-medium">平安银行</p>
                <p className="text-sm text-text-secondary">000001.SZ</p>
              </div>
            </div>
            <div className="text-right">
              <p className={cn("num font-medium", directionClass(delta))}>
                {loading ? "—" : formatPrice(price)}
              </p>
              {delta != null && prevClose != null && prevClose !== 0 && (
                <div
                  className={cn(
                    "flex items-center justify-end gap-1 text-sm",
                    directionClass(delta),
                  )}
                >
                  <TrendIcon className="h-3.5 w-3.5" />
                  <span className="num">
                    {formatPercent((delta / prevClose) * 100)}
                  </span>
                </div>
              )}
            </div>
          </div>
          <p className="mt-4 text-center text-xs text-flat">
            PR1b 接入完整持仓列表
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
