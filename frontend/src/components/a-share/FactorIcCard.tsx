/**
 * 策略体检卡（因子评测表）—— IC + pool-alpha + 4 态 verdict
 *
 * 数据源: GET /api/screener/factor-ic（读落盘 factor_ic.json）
 * 触发刷新: POST /api/screener/factor-ic/run（同步，慢）
 *
 * 字段形状以 apex/screener_backtest.py:run 返回的 factor_ic payload 为准。
 * verdict 4 态: n_insufficient / alpha_unavailable / dead_weight / live_candidate
 *
 * 常驻 screener 页，避免 CLI 工具被遗忘（功能可见 = 不会被忘）。
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Activity, RefreshCw, Loader2, AlertTriangle } from "lucide-react";
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
  getFactorIc,
  runFactorIc,
  type FactorIcPayload,
  type FactorIcRow,
} from "@/api/screener";
import { cn, directionClass } from "@/lib/utils";

const VERDICT_LABEL: Record<FactorIcRow["verdict"], string> = {
  n_insufficient: "样本不足",
  alpha_unavailable: "基准缺失",
  dead_weight: "疑似失效",
  live_candidate: "有效",
};

const VERDICT_BADGE: Record<FactorIcRow["verdict"], string> = {
  n_insufficient: "bg-bg-base text-text-secondary",
  alpha_unavailable: "bg-bg-base text-text-secondary",
  dead_weight: "bg-down/10 text-down",
  live_candidate: "bg-up/10 text-up",
};

function fmtNum(v: number | null, digits = 3): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}`;
}

function fmtPct(v: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${(v * 100).toFixed(digits)}%`;
}

export function FactorIcCard() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: qk.factorIc,
    queryFn: getFactorIc,
  });

  const runMut = useMutation({
    mutationFn: () => runFactorIc(true),
    onSuccess: (data) => {
      qc.setQueryData(qk.factorIc, data);
    },
  });

  const data = query.data ?? null;
  const running = runMut.isPending;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Activity className="h-4 w-4 text-text-secondary" />
        <CardTitle>策略体检</CardTitle>
        <CardDescription>
          {data
            ? `${data.reports_used}/${data.reports_scanned} 份报告可用 · ${data.generated_at.slice(0, 16)}`
            : "尚未体检"}
        </CardDescription>
        <Button
          variant="ghost"
          className="ml-auto"
          disabled={running}
          onClick={() => runMut.mutate()}
        >
          {running ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {running ? "体检中..." : "重新体检"}
        </Button>
      </CardHeader>
      <CardContent className="pt-0">
        {/* 样本警告 */}
        <div className="mb-3 flex items-start gap-2 rounded border border-text-secondary/20 bg-bg-base p-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-text-secondary" />
          <p className="text-[11px] leading-relaxed text-text-secondary">
            IC = 策略规则分对 N 日收益的排序预测力（-1~1，越接近 ±1 越准）。
            verdict 优先级: 样本不足 &gt; 基准缺失 &gt; 疑似失效 &gt; 有效。
            样本 N&lt;30 时结论当先验不当定论。
          </p>
        </div>

        {runMut.isError && (
          <p className="mb-2 text-xs text-down">
            体检失败: {(runMut.error as Error)?.message ?? "未知错误"}
          </p>
        )}

        {query.isLoading ? (
          <p className="py-4 text-center text-sm text-flat">加载中...</p>
        ) : !data ? (
          <p className="py-4 text-center text-sm text-flat">
            尚未体检。点击「重新体检」触发回测（首次较慢）。
          </p>
        ) : data.by_strategy.length === 0 ? (
          <p className="py-4 text-center text-sm text-flat">
            无可用报告（screener 报告需含 strategy 字段）。
          </p>
        ) : (
          <FactorIcTable data={data} />
        )}
      </CardContent>
    </Card>
  );
}

function FactorIcTable({ data }: { data: FactorIcPayload }) {
  const rows = data.by_strategy;
  const verdictCounts = rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.verdict] = (acc[r.verdict] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-secondary">
              <th className="py-2 pr-3 font-medium">策略</th>
              <th className="px-2 py-2 text-right font-medium num">n</th>
              <th className="px-2 py-2 text-right font-medium num">可回测</th>
              <th className="px-2 py-2 text-right font-medium num">IC5d</th>
              <th className="px-2 py-2 text-right font-medium num">alpha5d</th>
              <th className="px-2 py-2 text-right font-medium num">日期数</th>
              <th className="px-2 py-2 text-right font-medium">基准</th>
              <th className="pl-2 py-2 font-medium">结论</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-b border-border/50">
                <td className="py-2 pr-3 font-medium">{r.key}</td>
                <td className="num px-2 py-2 text-right text-text-secondary">{r.n}</td>
                <td className="num px-2 py-2 text-right text-text-secondary">{r.fillable_n}</td>
                <td className={cn("num px-2 py-2 text-right font-medium", directionClass(r.ic_5d))}>
                  {fmtNum(r.ic_5d)}
                </td>
                <td className={cn("num px-2 py-2 text-right", directionClass(r.pool_alpha_5d))}>
                  {fmtPct(r.pool_alpha_5d)}
                </td>
                <td className="num px-2 py-2 text-right text-text-secondary">
                  {r.n_dates_5d}/{data.thresholds.min_n_dates}
                </td>
                <td className="px-2 py-2 text-right text-xs text-text-secondary">
                  {r.benchmark_n > 0 ? r.benchmark_n : "—"}
                </td>
                <td className="pl-2 py-2">
                  <span
                    className={cn(
                      "inline-block rounded px-1.5 py-0.5 text-[11px] font-medium",
                      VERDICT_BADGE[r.verdict],
                    )}
                  >
                    {VERDICT_LABEL[r.verdict]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 汇总 */}
      <div className="flex flex-wrap gap-2 text-[11px]">
        {(["live_candidate", "dead_weight", "alpha_unavailable", "n_insufficient"] as const).map(
          (v) =>
            verdictCounts[v] ? (
              <span
                key={v}
                className={cn(
                  "rounded px-1.5 py-0.5 font-medium",
                  VERDICT_BADGE[v],
                )}
              >
                {VERDICT_LABEL[v]} {verdictCounts[v]}
              </span>
            ) : null,
        )}
      </div>
    </div>
  );
}
