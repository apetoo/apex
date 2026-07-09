import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Target,
  RefreshCw,
  Loader2,
  AlertTriangle,
  Activity,
  ShieldCheck,
  Gauge,
  Dna,
  TrendingDown,
} from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { qk } from "@/api/query-keys";
import {
  getSystem,
  recomputeSystem,
  type SystemView,
  type Confidence,
  type RateMetric,
} from "@/api/system";
import { cn } from "@/lib/utils";

/**
 * /system 我的交易系统（ADR-0001 双层守规 / ADR-0002 样本门控）
 *
 * v1 = 纪律镜像：Trading DNA + Behavior Analytics + AI 守规 + Discipline Score。
 * Tier-2（归因/挖规则/回测）以「累积中」状态存在，n 达阈后转结论。
 * 每个指标带 n + confidence 标志，低样本只描述不推断。
 */
export function SystemPage() {
  const qc = useQueryClient();
  const { data, isLoading, isFetching } = useQuery({
    queryKey: qk.system,
    queryFn: getSystem,
  });

  const recompute = useMutation({
    mutationFn: recomputeSystem,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.system }),
  });

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="font-serif text-3xl font-semibold tracking-tight">
            我的交易系统
          </h1>
          <p className="mt-1 text-sm text-text-secondary">
            纪律镜像 · 行为归因 · AI 守规 · 样本门控（n 达阈前只描述不推断）
          </p>
        </div>
        <Button
          variant="primary"
          onClick={() => recompute.mutate()}
          disabled={recompute.isPending}
        >
          {recompute.isPending ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {data ? "重算" : "生成视图"}
        </Button>
      </div>

      {isLoading && (
        <Card>
          <CardContent className="py-6 text-center text-sm text-flat">
            <Loader2 className="mx-auto mb-2 h-4 w-4 animate-spin" />
            加载中...
          </CardContent>
        </Card>
      )}

      {!isLoading && !data && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <Target className="h-8 w-8 text-text-secondary" />
            <p className="text-sm text-text-secondary">
              尚未生成交易系统视图。点击「生成视图」从历史持仓/流水/分析聚合。
            </p>
            {recompute.isError && (
              <p className="text-xs text-down">
                {(recompute.error as Error)?.message ?? "计算失败"}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <SampleBar data={data} />
          <div className="grid gap-4 lg:grid-cols-2">
            <TradingDnaCard data={data} />
            <BehaviorCard data={data} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <AiAdherenceCard data={data} />
            <DisciplineCard data={data} />
          </div>
          <Tier2Card data={data} />
        </>
      )}

      {isFetching && !isLoading && (
        <p className="text-center text-[11px] text-flat">刷新中…</p>
      )}
    </div>
  );
}

/* ── 样本条 + 门控横幅 ─────────────────────────────────────── */

function SampleBar({ data }: { data: SystemView }) {
  const s = data.sample;
  const items: Array<[string, number]> = [
    ["已平仓", s.closed_n],
    ["成交笔数", s.trades_n],
    ["分析条数", s.journal_n],
    ["可归因", s.eligible_n],
  ];
  const lowN = s.eligible_n < data.thresholds.expectancy_n_min;
  return (
    <Card>
      <CardContent className="py-4">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          {items.map(([label, n]) => (
            <div key={label} className="flex items-baseline gap-1.5">
              <span className="text-xs text-text-secondary">{label}</span>
              <span className="num text-lg font-semibold">{n}</span>
            </div>
          ))}
          <span className="num ml-auto text-[11px] text-flat">
            计算于 {data.computed_at?.slice(0, 19)}
          </span>
        </div>
        {lowN && (
          <div className="mt-3 flex items-start gap-2 rounded border border-amber-500/30 bg-amber-500/5 p-2.5">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-600" />
            <p className="text-xs text-text-secondary">
              <span className="font-medium text-amber-700">低样本（可归因 n={s.eligible_n}）</span>
              ：以下指标仅描述历史、不推断为「你的优势」。盈亏归因/规则挖掘需
              n≥{data.thresholds.expectancy_n_min} 才转结论（ADR-0002）。
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ── Trading DNA ─────────────────────────────────────────── */

function TradingDnaCard({ data }: { data: SystemView }) {
  const d = data.trading_dna;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Dna className="h-4 w-4 text-text-secondary" />
        <CardTitle>Trading DNA</CardTitle>
        <CardDescription>风格画像（描述）</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <DistRow label="方向分布" dist={d.verdict_distribution} />
        <DistRow label="策略来源" dist={d.strategy_distribution} />
        <DistRow
          label="Setup 分布"
          dist={d.setup_distribution}
          emptyHint="前向捕获后填充（ADR-0001）"
        />
        <DistRow
          label="Regime 分布"
          dist={d.regime_distribution}
          emptyHint="regime_at_open 回填后填充"
        />
        <DistRow
          label="板块分布"
          dist={d.sector_concentration.distribution}
          emptyHint="sector 回填后填充"
        />
        {d.sector_concentration.top && (
          <div className="flex items-center justify-between text-xs">
            <span className="text-text-secondary">最集中板块</span>
            <span className="num">
              {d.sector_concentration.top.name} ·{" "}
              {(d.sector_concentration.top.share * 100).toFixed(0)}%
              <ConfidenceBadge
                c={d.sector_concentration.confidence}
                n={d.sector_concentration.n}
                className="ml-2"
              />
            </span>
          </div>
        )}
        <div className="flex items-center justify-between border-t border-border pt-3">
          <span className="text-xs text-text-secondary">平均持仓天数</span>
          <span className="num text-sm font-medium">
            {d.avg_hold_days.value ?? "—"}
            <ConfidenceBadge c={d.avg_hold_days.confidence} n={d.avg_hold_days.n} className="ml-2" />
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Behavior Analytics ──────────────────────────────────── */

function BehaviorCard({ data }: { data: SystemView }) {
  const b = data.behavior;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Activity className="h-4 w-4 text-text-secondary" />
        <CardTitle>行为分析</CardTitle>
        <CardDescription>可观测行为 + 代理情绪</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2.5 pt-0">
        <RateRow label="追高（>MA5 且 +3%）" m={b.chase} />
        <RateRow label="补仓摊平" m={b.average_down} />
        <StopRow m={b.stop_discipline} />
        <RateRow
          label="止盈纪律（触目标未止盈）"
          m={{
            n: b.take_profit_discipline.n,
            count: b.take_profit_discipline.missed_n,
            rate:
              b.take_profit_discipline.n
                ? b.take_profit_discipline.missed_n / b.take_profit_discipline.n
                : null,
            confidence: b.take_profit_discipline.confidence,
          }}
        />
        <RateRow label="复仇交易（亏损后≤3日开仓）" m={b.proxy_emotional.revenge} warnHigh />
        <RateRow label="FOMO（超 AI 入场 5%）" m={b.proxy_emotional.fomo} warnHigh />
        <RateRow label="过度交易（单日>3笔）" m={b.proxy_emotional.overtrading} warnHigh />
      </CardContent>
    </Card>
  );
}

/* ── AI Adherence ────────────────────────────────────────── */

function AiAdherenceCard({ data }: { data: SystemView }) {
  const a = data.ai_adherence.aggregate;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <ShieldCheck className="h-4 w-4 text-text-secondary" />
        <CardTitle>AI 计划守规</CardTitle>
        <CardDescription>听 AI 的话？</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2.5 pt-0">
        <RatioRow label="入场带守规（±2%）" rate={a.entry_band_rate} />
        <RatioRow label="设了止损" rate={a.stop_set_rate} />
        <RatioRow label="触及止损后执行" rate={a.stop_honored_rate} warnHigh={false} />
        <div className="flex items-center justify-between border-t border-border pt-2">
          <span className="text-[11px] text-flat">样本</span>
          <span className="num text-[11px] text-flat">n={a.n}</span>
        </div>
        <ConfidenceBadge c={a.confidence} n={a.n} />
      </CardContent>
    </Card>
  );
}

/* ── Discipline Score ────────────────────────────────────── */

function DisciplineCard({ data }: { data: SystemView }) {
  const r = data.discipline_score.rolling_avg;
  const val = r.value;
  const color =
    val == null ? "text-flat" : val >= 0.7 ? "text-up" : val >= 0.4 ? "text-flat" : "text-down";
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Gauge className="h-4 w-4 text-text-secondary" />
        <CardTitle>纪律分</CardTitle>
        <CardDescription>守规满足率（0-1）</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="flex items-baseline gap-2">
          <span className={cn("num text-4xl font-semibold", color)}>
            {val ?? "—"}
          </span>
          <ConfidenceBadge c={r.confidence} n={r.n} />
        </div>
        <p className="text-[11px] text-flat">
          v1 = AI 计划守规分；自录 Rule 检查表有数据后混入自录层（ADR-0001）。
        </p>
        <div className="divide-y divide-border">
          {data.discipline_score.per_trade.map((p) => (
            <div key={p.ts_code} className="flex items-center justify-between py-1.5">
              <span className="truncate text-xs">
                {p.name || p.ts_code}
                {p.has_user_rule && (
                  <span className="ml-1 rounded bg-bg-base px-1 text-[10px] text-text-secondary">
                    自录规则
                  </span>
                )}
              </span>
              <span
                className={cn(
                  "num text-xs font-medium",
                  p.score == null
                    ? "text-flat"
                    : p.score >= 0.7
                      ? "text-up"
                      : p.score >= 0.4
                        ? "text-flat"
                        : "text-down",
                )}
              >
                {p.score ?? "—"}
              </span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Tier-2 累积中 ───────────────────────────────────────── */

function Tier2Card({ data }: { data: SystemView }) {
  const t = data.tier2_status;
  const items: Array<[string, unknown]> = [
    ["盈亏归因", t.performance_attribution],
    ["规则挖掘", t.rule_discovery],
    ["策略回测", t.strategy_simulator],
  ];
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <TrendingDown className="h-4 w-4 text-text-secondary" />
        <CardTitle>Tier-2（累积中）</CardTitle>
        <CardDescription>n 达阈后转结论</CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {items.map(([label, st]) => (
            <div
              key={label}
              className="rounded-md border border-border bg-bg-base p-3"
            >
              <p className="text-xs text-text-secondary">{label}</p>
              <p className="mt-1 text-sm font-medium text-flat">
                {typeof st === "string" ? tier2Label(st) : "—"}
              </p>
            </div>
          ))}
        </div>
        {typeof t.note === "string" && (
          <p className="mt-2 text-[11px] text-flat">{t.note}</p>
        )}
      </CardContent>
    </Card>
  );
}

/* ── 子组件 ──────────────────────────────────────────────── */

function ConfidenceBadge({
  c,
  n,
  className,
}: {
  c: Confidence;
  n?: number;
  className?: string;
}) {
  const map: Record<Confidence, { label: string; cls: string }> = {
    ok: { label: "OK", cls: "text-up border-up/30 bg-up/5" },
    low_sample_descriptive_only: {
      label: "低样本·仅描述",
      cls: "text-amber-700 border-amber-500/30 bg-amber-500/5",
    },
    accumulating: {
      label: "累积中",
      cls: "text-text-secondary border-border bg-bg-base",
    },
    no_data: { label: "无数据", cls: "text-flat border-border" },
  };
  const m = map[c] ?? map.no_data;
  return (
    <span
      className={cn(
        "num inline-flex items-center rounded border px-1.5 py-0.5 text-[10px]",
        m.cls,
        className,
      )}
    >
      {m.label}
      {n != null && c !== "ok" ? ` · n=${n}` : ""}
    </span>
  );
}

function RateRow({
  label,
  m,
  warnHigh = false,
}: {
  label: string;
  m: RateMetric;
  warnHigh?: boolean;
}) {
  const rate = m.rate;
  const bad = warnHigh && rate != null && rate > 0.3;
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="min-w-0">
        <p className="truncate text-xs">{label}</p>
        <p className="num text-[10px] text-flat">
          {m.count}/{m.n}
          {m.n ? ` · ${(rate ?? 0).toFixed(0)}` : ""}
          {rate != null && m.n ? "%" : ""}
        </p>
      </div>
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "num text-sm font-medium",
            bad ? "text-down" : rate != null && warnHigh ? "text-flat" : "",
          )}
        >
          {rate == null ? "—" : `${(rate * 100).toFixed(0)}%`}
        </span>
        <ConfidenceBadge c={m.confidence} n={m.n} />
      </div>
    </div>
  );
}

function RatioRow({
  label,
  rate,
  warnHigh = true,
}: {
  label: string;
  rate: number | null;
  warnHigh?: boolean;
}) {
  const bad = warnHigh && rate != null && rate < 0.5;
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs">{label}</span>
      <span
        className={cn(
          "num text-sm font-medium",
          rate == null ? "text-flat" : bad ? "text-down" : "text-up",
        )}
      >
        {rate == null ? "—" : `${(rate * 100).toFixed(0)}%`}
      </span>
    </div>
  );
}

function StopRow({
  m,
}: {
  m: SystemView["behavior"]["stop_discipline"];
}) {
  return (
    <div className="rounded-md border border-border bg-bg-base p-2.5">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs">止损纪律</span>
        <ConfidenceBadge c={m.confidence} n={m.n} />
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <StopCell label="未设止损" n={m.no_stop_n} tone="flat" />
        <StopCell label="触及后执行" n={m.honored_n} tone="up" />
        <StopCell label="越线硬扛" n={m.override_breach_n} tone="down" />
        <StopCell label="未触及" n={m.not_triggered_n} tone="flat" />
      </div>
    </div>
  );
}

function StopCell({
  label,
  n,
  tone,
}: {
  label: string;
  n: number;
  tone: "up" | "down" | "flat";
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-text-secondary">{label}</span>
      <span
        className={cn(
          "num font-medium",
          tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-flat",
        )}
      >
        {n}
      </span>
    </div>
  );
}

function DistRow({
  label,
  dist,
  emptyHint,
}: {
  label: string;
  dist: Record<string, number>;
  emptyHint?: string;
}) {
  const entries = Object.entries(dist).filter(([, v]) => v > 0);
  const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
  return (
    <div>
      <p className="mb-1 text-xs text-text-secondary">{label}</p>
      {entries.length === 0 ? (
        <p className="text-[11px] text-flat">{emptyHint ?? "无数据"}</p>
      ) : (
        <div className="space-y-1">
          {entries.map(([k, v]) => (
            <div key={k} className="flex items-center gap-2">
              <span className="w-20 shrink-0 truncate text-[11px]">{k}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded bg-bg-base">
                <div
                  className="h-full rounded bg-text-secondary/40"
                  style={{ width: `${(v / total) * 100}%` }}
                />
              </div>
              <span className="num w-6 shrink-0 text-right text-[11px]">{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function tier2Label(st: string): string {
  if (st === "ok") return "可结论";
  if (st === "accumulating") return "累积中";
  if (st === "low_sample_descriptive_only") return "低样本·仅描述";
  return "无数据";
}
