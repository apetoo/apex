import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, History, Target, ShieldAlert, Clock, CheckCircle2, XCircle } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { VerdictTag } from "@/components/a-share";
import { getBacktestSignals, getBacktestRealized, resolveExitReason } from "@/api/backtest";
import { cn, formatPercent, formatRatio } from "@/lib/utils";

/**
 * /backtest 回测页
 *
 * ED11(修正): 逐笔收益柱状图 + 统计表(不画错的净值曲线)。
 * 统计: 胜率 / 平均净收益 / 平均超额 / 平均回撤 / 平均夏普。
 *
 * 1:1 对应 streamlit tab_bt:
 *   - 选 ts_code + lookforward_days
 *   - 信号列表(逐笔交易)
 *   - 净值曲线(改: 逐笔柱状图)
 *   - 已实现收益(实盘平仓)
 *
 * 柱状图: 纯 SVG(不引图表库, ED11 决策轻量)
 */
export function BacktestPage() {
  const [tsCode, setTsCode] = useState("");
  const [committedCode, setCommittedCode] = useState<string | null>(null);
  const [lookforwardDays, setLookforwardDays] = useState(10);

  const signals = useQuery({
    queryKey: ["backtest", "signals", committedCode, lookforwardDays],
    queryFn: () => getBacktestSignals(committedCode, lookforwardDays),
  });
  const realized = useQuery({
    queryKey: ["backtest", "realized"],
    queryFn: getBacktestRealized,
  });

  const data = signals.data ?? [];
  const stats = computeStats(data);

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">
          回测
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          AI 多头信号的逐笔 P&amp;L · 统计表 · 实盘平仓复盘
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
          <Button
            variant="primary"
            onClick={() => setCommittedCode(tsCode.trim() || null)}
          >
            跑回测
          </Button>
        </CardContent>
      </Card>

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

      {/* 逐笔收益柱状图(ED11 修正) */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <BarChart3 className="h-4 w-4 text-text-secondary" />
          <CardTitle>逐笔收益</CardTitle>
          <CardDescription>
            {data.length} 笔 · ED11 不画错的净值曲线, 用柱状图直观对比
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
                // 真后端字段优先(更准): fill_price / net_pnl_pct / days_held
                // 老 mock 字段(向后兼容): entry_price / net_return / hold_days
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
