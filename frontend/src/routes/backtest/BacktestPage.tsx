import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  History,
  Target,
  ShieldAlert,
  Clock,
  CheckCircle2,
  XCircle,
  TrendingUp,
  Gauge,
  Activity,
  Sparkles,
  AlertTriangle,
} from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { VerdictTag } from "@/components/a-share";
import {
  getBacktestSignals,
  getBacktestRealized,
  getBacktestSweep,
  getBacktestAggregate,
  getBacktestPortfolio,
  reviewBacktest,
  resolveExitReason,
  type SweepByPeriod,
  type AggregateBucket,
  type AggregateResult,
  type PortfolioResult,
  type BacktestReviewResult,
  type ReviewSeverity,
  type WeightHint,
} from "@/api/backtest";
import { ApiError } from "@/api/client";
import { cn, formatPercent, formatRatio } from "@/lib/utils";

/**
 * /backtest 回测页
 *
 * 五个视图:
 *  - 组合净值(P4): 全部信号喂进单个 Portfolio(共享资金池) → 净值曲线 + stats
 *  - 持有期扫描(P2): 每条信号 × 多档持有期 → 最优持有几天
 *  - AI 校准(P3): 按 置信度桶/verdict/source 切片 → AI 自信时准不准
 *  - 逐笔收益(ED11): 柱状图(不画客户端累乘净值, 组合净值已由 P4 服务端算)
 *  - 实盘平仓: 真实成交
 *
 * A 股: 红涨绿跌。图表纯 SVG(ED11 决策轻量)。
 */
export function BacktestPage() {
  const [tsCode, setTsCode] = useState("");
  const [committedCode, setCommittedCode] = useState<string | null>(null);
  const [lookforwardDays, setLookforwardDays] = useState(10);
  // 门控: 进页面不自动跑回测(原来 committedCode 初始 null 触发全量回测),
  // 点「跑回测」才置 true, 4 个回测 query 的 enabled 都依赖它。
  const [hasRun, setHasRun] = useState(false);

  const signals = useQuery({
    queryKey: ["backtest", "signals", committedCode, lookforwardDays],
    queryFn: () => getBacktestSignals(committedCode, lookforwardDays),
    enabled: hasRun,
  });
  const realized = useQuery({
    queryKey: ["backtest", "realized"],
    queryFn: getBacktestRealized,
  });
  const sweep = useQuery({
    queryKey: ["backtest", "sweep", committedCode],
    queryFn: () => getBacktestSweep(committedCode),
    enabled: hasRun,
  });
  const aggregate = useQuery({
    queryKey: ["backtest", "aggregate", committedCode, lookforwardDays],
    queryFn: () => getBacktestAggregate(committedCode, lookforwardDays),
    enabled: hasRun,
  });
  const portfolio = useQuery({
    queryKey: ["backtest", "portfolio", committedCode, lookforwardDays],
    queryFn: () => getBacktestPortfolio(committedCode, lookforwardDays),
    enabled: hasRun,
  });

  const data = signals.data ?? [];
  const stats = computeStats(data);

  const triggerRun = () => {
    setCommittedCode(tsCode.trim() || null);
    setHasRun(true);
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">
          回测
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          组合净值 · 持有期扫描 · AI 校准 · 逐笔 P&amp;L · 实盘平仓
        </p>
      </div>

      {/* 控制区 */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 py-4">
          <div className="min-w-[180px] flex-1">
            <label className="mb-1 block text-xs text-text-secondary">
              标的(留空=全部)
            </label>
            <input
              type="text"
              value={tsCode}
              onChange={(e) => setTsCode(e.target.value.toUpperCase())}
              placeholder="002466.SZ"
              className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
            />
          </div>
          <div className="min-w-[140px]">
            <label className="mb-1 block text-xs text-text-secondary">
              前瞻天数
            </label>
            <input
              type="number"
              min={1}
              max={60}
              value={lookforwardDays}
              onChange={(e) =>
                setLookforwardDays(Math.max(1, Math.min(60, Number(e.target.value))))
              }
              className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
            />
          </div>
          <Button variant="primary" onClick={triggerRun}>
            跑回测
          </Button>
        </CardContent>
      </Card>

      {/* 未运行占位: 进页面不自动回测, 提示点按钮 */}
      {!hasRun && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-flat">
            选好标的与前瞻天数, 点上方「跑回测」开始
          </CardContent>
        </Card>
      )}

      {/* 回测结果区: 未运行时不渲染, 避免空态误导 */}
      {hasRun && (
        <>
      {/* 统计卡 */}
      {data.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <StatCard label="信号数" value={String(data.length)} />
          <StatCard
            label="胜率"
            value={formatRatio(stats.winRate)}
            tone={stats.winRate >= 0.5 ? "up" : "down"}
          />
          <StatCard
            label="平均净收益"
            value={formatPercent(stats.avgNet)}
            tone={stats.avgNet >= 0 ? "up" : "down"}
          />
          <StatCard
            label="平均超额"
            value={
              stats.avgExcess == null ? "—" : formatPercent(stats.avgExcess)
            }
            tone={
              stats.avgExcess == null
                ? "flat"
                : stats.avgExcess >= 0
                  ? "up"
                  : "down"
            }
          />
          <StatCard
            label="平均夏普"
            value={stats.avgSharpe?.toFixed(2) ?? "—"}
            tone="flat"
          />
        </div>
      )}

      {/* P4: 组合净值 */}
      <PortfolioCard query={portfolio} />

      {/* P2: 持有期扫描 */}
      <SweepCard query={sweep} />

      {/* P3: AI 校准 */}
      <AggregateCard query={aggregate} />

      {/* AI 复盘: 对 aggregate 切片跑 DeepSeek，产出可执行结论 + prompt_injection */}
      <ReviewCard tsCode={committedCode} lookforwardDays={lookforwardDays} />

      {/* 逐笔收益柱状图(ED11 修正) */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <BarChart3 className="h-4 w-4 text-text-secondary" />
          <CardTitle>逐笔收益</CardTitle>
          <CardDescription>
            {data.length} 笔 · T+1 开盘入场 · 涨停不可成交已剔除
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          {signals.isLoading ? (
            <p className="py-8 text-center text-sm text-flat">回测中...</p>
          ) : data.length === 0 ? (
            <p className="py-8 text-center text-sm text-flat">
              无信号 · 调大 lookforward 或换标的
            </p>
          ) : (
            <BarChart signals={data} />
          )}
        </CardContent>
      </Card>

      {/* 信号列表 */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <History className="h-4 w-4 text-text-secondary" />
          <CardTitle>信号明细</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          {data.length === 0 ? (
            <p className="py-4 text-center text-sm text-flat">暂无信号</p>
          ) : (
            <div className="divide-y divide-border">
              {data.map((s, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate font-medium">{s.name ?? s.ts_code}</p>
                      <VerdictTag verdict={s.verdict} />
                      {s.hit ? (
                        <CheckCircle2 className="h-3.5 w-3.5 text-up" />
                      ) : (
                        <XCircle className="h-3.5 w-3.5 text-down" />
                      )}
                    </div>
                    <p className="num mt-0.5 text-xs text-text-secondary">
                      {s.ts_code} · {s.date} · 入场 {s.fill_price?.toFixed(2)}
                    </p>
                  </div>
                  <div className="text-right">
                    <p
                      className={cn(
                        "num font-medium",
                        s.net_return >= 0 ? "text-up" : "text-down",
                      )}
                    >
                      {formatPercent(s.net_return)}
                    </p>
                    {s.excess_return != null && (
                      <p
                        className={cn(
                          "num mt-0.5 text-xs",
                          s.excess_return >= 0 ? "text-up" : "text-down",
                        )}
                      >
                        超额 {formatPercent(s.excess_return)}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
        </>
      )}

      {/* 实盘平仓 */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <Target className="h-4 w-4 text-text-secondary" />
          <CardTitle>实盘平仓</CardTitle>
          <CardDescription>
            真实成交 · 含佣金印花税 · 来自{" "}
            <code className="num">/api/backtest/realized</code>
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          {realized.isLoading ? (
            <p className="py-4 text-center text-sm text-flat">加载中...</p>
          ) : (realized.data ?? []).length === 0 ? (
            <p className="py-4 text-center text-sm text-flat">暂无实盘</p>
          ) : (
            <div className="divide-y divide-border">
              {(realized.data ?? []).map((r, i) => {
                const fillPrice = r.fill_price ?? r.entry_price ?? null;
                const netReturn = r.net_pnl_pct ?? r.net_return ?? 0;
                const heldDays = r.days_held ?? r.hold_days ?? 0;
                const reason = resolveExitReason(r.exit_reason);
                const ReasonIcon =
                  reason.tone === "up"
                    ? Target
                    : reason.tone === "down"
                      ? ShieldAlert
                      : Clock;
                return (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">{r.name ?? r.ts_code}</p>
                    <p className="num mt-0.5 text-xs text-text-secondary">
                      {r.ts_code} · {r.entry_date} → {r.exit_date} ·{" "}
                      {fillPrice != null ? fillPrice.toFixed(2) : "—"} →{" "}
                      {r.exit_price.toFixed(2)}
                    </p>
                    <div className="mt-1 flex items-center gap-2 text-[11px] text-text-secondary">
                      <ReasonIcon
                        className={cn(
                          "h-3 w-3",
                          reason.tone === "up" && "text-up",
                          reason.tone === "down" && "text-down",
                        )}
                      />
                      <span>{reason.label}</span>
                      <span>· 持仓 {heldDays} 天</span>
                    </div>
                  </div>
                  <p
                    className={cn(
                      "num text-right font-medium",
                      netReturn >= 0 ? "text-up" : "text-down",
                    )}
                  >
                    {formatPercent(netReturn)}
                  </p>
                </div>
              );})}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* ── P4: 组合净值 ──────────────────────────────────────────── */

function PortfolioCard({
  query,
}: {
  query: ReturnType<typeof useQuery<PortfolioResult>>;
}) {
  const d = query.data;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2">
        <TrendingUp className="h-4 w-4 text-text-secondary" />
        <CardTitle>组合净值</CardTitle>
        <CardDescription>
          全部信号喂进单个 Portfolio（共享资金池 · 每笔 20% 仓位 · T+1 开盘成交）·
          服务端 vectorbt 算，非客户端累乘
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        {query.isLoading ? (
          <p className="py-8 text-center text-sm text-flat">回测中...</p>
        ) : !d || d.equity_curve.length === 0 ? (
          <p className="py-8 text-center text-sm text-flat">
            {d?.error ?? "无信号或数据不足"}
          </p>
        ) : (
          <div className="space-y-4">
            <EquityChart points={d.equity_curve} />
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <StatCard
                label="总收益"
                value={formatPercent(d.stats.total_return ?? 0)}
                tone={(d.stats.total_return ?? 0) >= 0 ? "up" : "down"}
              />
              <StatCard
                label="最大回撤"
                value={formatPercent(d.stats.max_drawdown ?? 0)}
                tone="down"
              />
              <StatCard label="夏普" value={(d.stats.sharpe ?? 0).toFixed(2)} />
              <StatCard label="交易数" value={String(d.stats.n_trades ?? 0)} />
              <StatCard
                label="胜率"
                value={formatRatio(d.stats.win_rate ?? 0)}
                tone={(d.stats.win_rate ?? 0) >= 0.5 ? "up" : "down"}
              />
              <StatCard
                label="终值"
                value={
                  d.stats.final_equity != null
                    ? d.stats.final_equity.toLocaleString()
                    : "—"
                }
              />
            </div>
            {d.trades.length > 0 && (
              <div className="divide-y divide-border">
                {d.trades.slice(0, 12).map((t, i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between gap-3 py-2"
                  >
                    <p className="num text-xs text-text-secondary">
                      {t.ts_code} · {t.entry_date} → {t.exit_date}
                    </p>
                    <p
                      className={cn(
                        "num text-sm font-medium",
                        t.return >= 0 ? "text-up" : "text-down",
                      )}
                    >
                      {formatPercent(t.return)}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function EquityChart({
  points,
}: {
  points: { date: string; equity: number }[];
}) {
  const W = 720;
  const H = 220;
  const PAD = { l: 56, r: 12, t: 12, b: 28 };
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;
  const eqs = points.map((p) => p.equity);
  const min = Math.min(...eqs);
  const max = Math.max(...eqs);
  const range = max - min || 1;
  const x = (i: number) => PAD.l + (i / (points.length - 1 || 1)) * innerW;
  const y = (v: number) => PAD.t + (1 - (v - min) / range) * innerH;
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`)
    .join(" ");
  const up = eqs[eqs.length - 1] >= eqs[0];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="组合净值曲线">
      {/* 基线(起始值) */}
      <line
        x1={PAD.l}
        x2={W - PAD.r}
        y1={y(eqs[0])}
        y2={y(eqs[0])}
        stroke="currentColor"
        className="text-border"
        strokeDasharray="3 3"
        strokeWidth={1}
      />
      <path d={path} fill="none" stroke="var(--color-up)" strokeWidth={1.8} />
      {/* 起止标注 */}
      <text x={PAD.l - 6} y={y(eqs[0]) + 3} textAnchor="end" className="fill-text-secondary text-[10px]">
        {eqs[0].toLocaleString()}
      </text>
      <text x={W - PAD.r} y={y(eqs[eqs.length - 1]) - 4} textAnchor="end" className="fill-text-secondary text-[10px]">
        {eqs[eqs.length - 1].toLocaleString()}
      </text>
      <text x={PAD.l} y={H - 8} className="fill-flat text-[10px]">
        {points[0].date}
      </text>
      <text x={W - PAD.r} y={H - 8} textAnchor="end" className="fill-flat text-[10px]">
        {points[points.length - 1].date}
      </text>
      <text x={W - PAD.r} y={PAD.t + 10} textAnchor="end" className={cn("text-[10px]", up ? "fill-up" : "fill-down")}>
        {up ? "▲" : "▼"} {((eqs[eqs.length - 1] / eqs[0] - 1) * 100).toFixed(2)}%
      </text>
    </svg>
  );
}

/* ── P2: 持有期扫描 ────────────────────────────────────────── */

function SweepCard({
  query,
}: {
  query: ReturnType<typeof useQuery<{ per_signal: unknown[]; by_period: SweepByPeriod[] }>>;
}) {
  const rows = query.data?.by_period ?? [];
  const fillable = rows.filter((r) => r.avg_net_return != null);
  const best =
    fillable.length === 0
      ? null
      : fillable.reduce((m, r) =>
          (r.avg_net_return ?? -Infinity) > (m.avg_net_return ?? -Infinity) ? r : m,
        ).holding_period;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2">
        <Activity className="h-4 w-4 text-text-secondary" />
        <CardTitle>持有期扫描</CardTitle>
        <CardDescription>
          每条信号 × 多档持有期 · 回答"AI 信号最优持有几天"
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        {query.isLoading ? (
          <p className="py-6 text-center text-sm text-flat">回测中...</p>
        ) : rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-flat">无信号</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="num w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-text-secondary">
                  <th className="py-2 pr-3 font-normal">持有天数</th>
                  <th className="py-2 pr-3 font-normal">可成交</th>
                  <th className="py-2 pr-3 font-normal">不可成交</th>
                  <th className="py-2 pr-3 font-normal">胜率</th>
                  <th className="py-2 pr-3 font-normal">平均净收益</th>
                  <th className="py-2 pr-3 font-normal">平均超额</th>
                  <th className="py-2 pr-3 font-normal">平均回撤</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const isBest = r.holding_period === best;
                  return (
                    <tr
                      key={r.holding_period}
                      className={cn("border-b border-border/50", isBest && "bg-up/5")}
                    >
                      <td className="py-2 pr-3 font-medium">
                        {r.holding_period}
                        {isBest && <span className="ml-1 text-up">★</span>}
                      </td>
                      <td className="py-2 pr-3 text-text-secondary">{r.fillable_n}</td>
                      <td className="py-2 pr-3 text-text-secondary">
                        {r.unfillable_count || "—"}
                      </td>
                      <td
                        className={cn(
                          "py-2 pr-3",
                          r.win_rate == null
                            ? "text-flat"
                            : r.win_rate >= 0.5
                              ? "text-up"
                              : "text-down",
                        )}
                      >
                        {r.win_rate == null ? "—" : formatRatio(r.win_rate)}
                      </td>
                      <td
                        className={cn(
                          "py-2 pr-3",
                          r.avg_net_return == null
                            ? "text-flat"
                            : r.avg_net_return >= 0
                              ? "text-up"
                              : "text-down",
                        )}
                      >
                        {r.avg_net_return == null ? "—" : formatPercent(r.avg_net_return)}
                      </td>
                      <td
                        className={cn(
                          "py-2 pr-3",
                          r.avg_excess_return == null
                            ? "text-flat"
                            : r.avg_excess_return >= 0
                              ? "text-up"
                              : "text-down",
                        )}
                      >
                        {r.avg_excess_return == null ? "—" : formatPercent(r.avg_excess_return)}
                      </td>
                      <td className="py-2 pr-3 text-down">
                        {r.avg_max_drawdown == null ? "—" : formatPercent(r.avg_max_drawdown)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ── P3: AI 校准 ───────────────────────────────────────────── */

function AggregateCard({
  query,
}: {
  query: ReturnType<typeof useQuery<AggregateResult>>;
}) {
  const d = query.data;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2">
        <Gauge className="h-4 w-4 text-text-secondary" />
        <CardTitle>AI 校准</CardTitle>
        <CardDescription>
          按 置信度桶 / verdict / source 切片 · 回答"AI 自信时准不准"
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        {query.isLoading ? (
          <p className="py-6 text-center text-sm text-flat">回测中...</p>
        ) : !d || d.fillable_count === 0 ? (
          <p className="py-6 text-center text-sm text-flat">无信号</p>
        ) : (
          <div className="space-y-4">
            <p className="text-xs text-text-secondary">
              共 {d.total_signals} 条信号 · 可成交 {d.fillable_count}
              {d.unfillable_count > 0 && (
                <span className="text-down">
                  {" "}· 涨停不可成交 {d.unfillable_count}（已剔出胜率分母）
                </span>
              )}
            </p>
            <div className="grid gap-4 sm:grid-cols-3">
              <BucketTable
                title="置信度桶"
                buckets={d.by_confidence_bucket}
                highlight
              />
              <BucketTable title="Verdict" buckets={d.by_verdict} />
              <BucketTable title="来源" buckets={d.by_strategy} />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function BucketTable({
  title,
  buckets,
  highlight = false,
}: {
  title: string;
  buckets: AggregateBucket[];
  highlight?: boolean;
}) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-text-secondary">{title}</p>
      <table className="num w-full text-xs">
        <thead>
          <tr className="border-b border-border text-left text-text-secondary">
            <th className="py-1.5 pr-2 font-normal">桶</th>
            <th className="py-1.5 pr-2 font-normal">N</th>
            <th className="py-1.5 pr-2 font-normal">胜率</th>
            <th className="py-1.5 font-normal">净收益</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => (
            <tr key={b.key} className="border-b border-border/40">
              <td className="py-1.5 pr-2">{b.key}</td>
              <td className="py-1.5 pr-2 text-text-secondary">{b.n}</td>
              <td
                className={cn(
                  "py-1.5 pr-2",
                  b.win_rate == null
                    ? "text-flat"
                    : b.win_rate >= 0.5
                      ? "text-up"
                      : "text-down",
                )}
              >
                {b.win_rate == null ? "—" : formatRatio(b.win_rate)}
              </td>
              <td
                className={cn(
                  "py-1.5",
                  b.avg_net_return == null
                    ? "text-flat"
                    : b.avg_net_return >= 0
                      ? "text-up"
                      : "text-down",
                )}
              >
                {b.avg_net_return == null ? "—" : formatPercent(b.avg_net_return)}
              </td>
            </tr>
          ))}
          {buckets.length === 0 && (
            <tr>
              <td colSpan={4} className="py-2 text-center text-flat">
                —
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {highlight && buckets.length >= 2 && (
        <CalibInsight buckets={buckets} />
      )}
    </div>
  );
}

function CalibInsight({ buckets }: { buckets: AggregateBucket[] }) {
  // 比较 7-10 vs 1-3 的胜率，给一句话结论
  const hi = buckets.find((b) => b.key === "7-10");
  const lo = buckets.find((b) => b.key === "1-3");
  if (!hi || !lo || hi.win_rate == null || lo.win_rate == null) return null;
  const diff = hi.win_rate - lo.win_rate;
  const good = diff > 0.05;
  return (
    <p
      className={cn(
        "mt-1.5 text-[11px]",
        good ? "text-up" : diff < -0.05 ? "text-down" : "text-flat",
      )}
    >
      {good
        ? `✓ 高置信(7-10)胜率 ${formatRatio(hi.win_rate)} 显著高于低置信(1-3) ${formatRatio(lo.win_rate)}，AI 自评有区分度`
        : diff < -0.05
          ? `✗ 高置信胜率反而更低，AI 自评失真`
          : `高/低置信胜率接近，AI 自评区分度有限`}
    </p>
  );
}

/* ── AI 复盘 ──────────────────────────────────────────────── */

const SEVERITY_STYLE: Record<ReviewSeverity, string> = {
  high: "text-down border-down/40 bg-down/5",
  medium: "text-amber-600 border-amber-500/40 bg-amber-500/5",
  low: "text-text-secondary border-border bg-bg-elevated",
};

const SEVERITY_LABEL: Record<ReviewSeverity, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

const CATEGORY_LABEL: Record<string, string> = {
  calibration: "置信度校准",
  exit: "止损止盈",
  strategy: "策略来源",
  regime: "市场环境",
  risk: "风控仓位",
  entry: "入场点",
};

const WEIGHT_STYLE: Record<WeightHint, string> = {
  increase: "text-up",
  decrease: "text-down",
  hold: "text-text-secondary",
};

const WEIGHT_LABEL: Record<WeightHint, string> = {
  increase: "↑ 增权",
  decrease: "↓ 降权",
  hold: "= 持平",
};

function ReviewCard({
  tsCode,
  lookforwardDays,
}: {
  tsCode: string | null;
  lookforwardDays: number;
}) {
  const mutation = useMutation({
    mutationFn: () => reviewBacktest(tsCode, lookforwardDays),
  });
  const d = mutation.data;
  const err =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.error instanceof Error
        ? mutation.error.message
        : null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2">
        <Sparkles className="h-4 w-4 text-text-secondary" />
        <CardTitle>AI 复盘</CardTitle>
        <CardDescription>
          把校准切片喂给 DeepSeek · 诊断系统性问题 + 反哺下次分析
        </CardDescription>
        <div className="ml-auto">
          <Button
            variant="primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "复盘中…" : "跑 AI 复盘"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {mutation.isIdle && !d && (
          <p className="py-6 text-center text-sm text-flat">
            点击「跑 AI 复盘」· 基于当前标的/前瞻天数的 aggregate 切片
          </p>
        )}
        {mutation.isPending && (
          <p className="py-6 text-center text-sm text-flat">
            DeepSeek 分析中（约数秒）…
          </p>
        )}
        {err && !mutation.isPending && (
          <p className="flex items-center justify-center gap-1.5 py-4 text-sm text-down">
            <AlertTriangle className="h-4 w-4" />
            {err}
          </p>
        )}
        {d && !mutation.isPending && (
          <div className="space-y-4">
            <p className="text-sm leading-relaxed">{d.summary}</p>

            {d.findings.length > 0 && (
              <div className="space-y-2">
                <p className="text-xs font-medium text-text-secondary">
                  发现的问题（{d.findings.length}）
                </p>
                {d.findings.map((f, i) => (
                  <div
                    key={i}
                    className={cn(
                      "rounded-md border px-3 py-2",
                      SEVERITY_STYLE[f.severity],
                    )}
                  >
                    <div className="flex items-center gap-2 text-xs">
                      <span className="font-medium">
                        {CATEGORY_LABEL[f.category] ?? f.category}
                      </span>
                      <span className="rounded bg-bg-elevated px-1.5 py-0.5">
                        {SEVERITY_LABEL[f.severity]}
                      </span>
                    </div>
                    <p className="mt-1 text-sm">{f.description}</p>
                    <p className="mt-1 text-xs text-text-secondary">
                      建议：{f.suggestion}
                    </p>
                  </div>
                ))}
              </div>
            )}

            {d.prompt_injection && (
              <div className="rounded-md border border-border bg-bg-elevated px-3 py-2">
                <p className="text-xs font-medium text-text-secondary">
                  下次分析注入提醒（已落盘，analyze 自动读取）
                </p>
                <p className="mt-1 text-sm leading-relaxed">
                  {d.prompt_injection}
                </p>
              </div>
            )}

            {Object.keys(d.strategy_weight_hint).length > 0 && (
              <div>
                <p className="mb-1.5 text-xs font-medium text-text-secondary">
                  策略权重建议（已落盘 backtest_strategy_stats.json）
                </p>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(d.strategy_weight_hint).map(([src, hint]) => (
                    <span
                      key={src}
                      className="num rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs"
                    >
                      {src}{" "}
                      <span className={WEIGHT_STYLE[hint]}>
                        {WEIGHT_LABEL[hint]}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            <p className="text-[11px] text-flat">
              模型 {d.model ?? "—"} · {d.generated_at}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ── 统计计算 ──────────────────────────────────────────────── */

interface Stats {
  count: number;
  winRate: number;
  avgNet: number;
  avgExcess: number | null;
  avgSharpe: number | null;
}

function computeStats(
  data: Array<{ net_return: number; excess_return?: number | null; sharpe?: number | null; hit: boolean }>,
): Stats {
  if (data.length === 0) {
    return { count: 0, winRate: 0, avgNet: 0, avgExcess: null, avgSharpe: null };
  }
  const hits = data.filter((d) => d.hit).length;
  const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const nets = data.map((d) => d.net_return);
  const excesses = data
    .map((d) => d.excess_return)
    .filter((x): x is number => x != null);
  const sharpes = data
    .map((d) => d.sharpe)
    .filter((x): x is number => x != null);
  return {
    count: data.length,
    winRate: hits / data.length,
    avgNet: avg(nets),
    avgExcess: excesses.length > 0 ? avg(excesses) : null,
    avgSharpe: sharpes.length > 0 ? avg(sharpes) : null,
  };
}

/* ── 柱状图(纯 SVG, ED11 决策轻量) ─────────────────────────── */

function BarChart({
  signals,
}: {
  signals: Array<{
    date: string;
    name?: string;
    ts_code: string;
    net_return: number;
  }>;
}) {
  const W = 720;
  const H = 200;
  const PAD = { l: 32, r: 12, t: 12, b: 32 };
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;
  const barW = (innerW / signals.length) * 0.7;
  const gap = (innerW / signals.length) * 0.3;

  const max = Math.max(...signals.map((s) => s.net_return), 0.01);
  const min = Math.min(...signals.map((s) => s.net_return), -0.01);
  const range = max - min || 0.02;
  const zeroY = PAD.t + (max / range) * innerH;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full"
      role="img"
      aria-label="逐笔收益柱状图"
    >
      {/* 0 基线 */}
      <line
        x1={PAD.l}
        x2={W - PAD.r}
        y1={zeroY}
        y2={zeroY}
        stroke="currentColor"
        className="text-border"
        strokeWidth={1}
      />
      {signals.map((s, i) => {
        const x = PAD.l + i * (barW + gap) + gap / 2;
        const h = (Math.abs(s.net_return) / range) * innerH;
        const y = s.net_return >= 0 ? zeroY - h : zeroY;
        const color = s.net_return >= 0 ? "var(--color-up)" : "var(--color-down)";
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={h}
              fill={color}
              rx={2}
            >
              <title>
                {s.name ?? s.ts_code} {s.date}: {(s.net_return * 100).toFixed(2)}%
              </title>
            </rect>
            <text
              x={x + barW / 2}
              y={H - PAD.b + 12}
              textAnchor="middle"
              className="fill-flat text-[10px]"
            >
              {s.date.slice(5)}
            </text>
          </g>
        );
      })}
      {/* 涨/跌 legend */}
      <g transform={`translate(${W - PAD.r - 100}, ${PAD.t})`}>
        <rect x={0} y={0} width={10} height={10} fill="var(--color-up)" rx={2} />
        <text x={14} y={9} className="fill-text-secondary text-[10px]">
          盈利
        </text>
        <rect x={48} y={0} width={10} height={10} fill="var(--color-down)" rx={2} />
        <text x={62} y={9} className="fill-text-secondary text-[10px]">
          亏损
        </text>
      </g>
    </svg>
  );
}

/* ── 统计小卡 ──────────────────────────────────────────────── */

function StatCard({
  label,
  value,
  tone = "flat",
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | "flat";
}) {
  return (
    <Card>
      <CardContent className="py-3">
        <p className="text-xs text-text-secondary">{label}</p>
        <p
          className={cn(
            "num mt-1 text-xl font-semibold",
            tone === "up" && "text-up",
            tone === "down" && "text-down",
          )}
        >
          {value}
        </p>
      </CardContent>
    </Card>
  );
}
