import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ScanSearch,
  Play,
  Loader2,
  Square,
  AlertCircle,
  RefreshCw,
  Sparkles,
  TrendingUp,
  Calendar,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { useSSE } from "@/hooks/useSSE";
import { useChatContext } from "@/hooks/useChatContext";
import {
  getStrategies,
  getDefaultWeights,
  screenerFetchFn,
  loadStoredWeights,
  saveWeights,
  getAvailableDates,
  type ScreenerReport,
  type ScreenerWeights,
  type ScreenerStrategy,
} from "@/api/screener";
import { cn, formatRatio } from "@/lib/utils";

/**
 * /screener 今日粗筛
 *
 * 1:1 对应 streamlit tab_screen:
 *   - 日期选择 + 权重配置(sliders)
 *   - 跑粗筛(SSE 进度 trace.data.type=progress)
 *   - 报告(按 score 排序的 picks + summary + AI summary)
 *
 * ED16: 权重存 localStorage, 刷新不丢
 * ED8: SSE 不依赖 MSW, dev mock 走 fetchFn 注入
 */
export function ScreenerPage() {
  const { setContext } = useChatContext();
  const [date, setDate] = useState("");
  const [weights, setWeights] = useState<ScreenerWeights>({});
  const [weightsLoaded, setWeightsLoaded] = useState(false);
  const [progressMessages, setProgressMessages] = useState<string[]>([]);

  // 策略 + 默认权重
  const strategies = useQuery({
    queryKey: ["screener", "strategies"],
    queryFn: getStrategies,
  });
  const defaultWeights = useQuery({
    queryKey: ["screener", "default-weights"],
    queryFn: getDefaultWeights,
  });
  const availableDates = useQuery({
    queryKey: ["screener", "dates"],
    queryFn: getAvailableDates,
  });

  // 初始化: 优先用 localStorage 的权重, 否则用默认
  useEffect(() => {
    if (weightsLoaded) return;
    if (defaultWeights.data) {
      const stored = loadStoredWeights(defaultWeights.data);
      setWeights(stored);
      setWeightsLoaded(true);
    }
  }, [defaultWeights.data, weightsLoaded]);

  // 持久化权重变化
  useEffect(() => {
    if (weightsLoaded && Object.keys(weights).length > 0) {
      saveWeights(weights);
    }
  }, [weights, weightsLoaded]);

  // SSE 流式进度
  const { events, status, error, result, connect, abort, reset } = useSSE<{
    type?: string;
    message?: string;
  } | ScreenerReport>();

  // 累积 progress 消息
  useEffect(() => {
    if (events.length === 0) return;
    const lastTrace = [...events].reverse().find((e) => e.event === "trace");
    if (lastTrace) {
      const data = lastTrace.data as { type?: string; message?: string };
      if (data?.type === "progress" && data.message) {
        setProgressMessages((m) => [...m, data.message as string]);
      }
    }
  }, [events]);

  // 终态: 注入 chat 上下文
  useEffect(() => {
    const report = result as ScreenerReport | null;
    if (report) {
      setContext(
        [
          `今日粗筛(${report.date})`,
          `扫描 ${report.summary.total_screened} 只, 通过 ${report.summary.passed} 只`,
          `Top picks: ${report.picks
            .slice(0, 3)
            .map((p) => `${p.name}(${(p.score * 100).toFixed(0)})`)
            .join(", ")}`,
          `AI 摘要: ${report.ai_summary ?? "—"}`,
        ].join("\n"),
      );
    }
  }, [result, setContext]);

  const running = status === "connecting" || status === "streaming";
  const report = (result ?? null) as ScreenerReport | null;

  const startScreener = () => {
    setProgressMessages([]);
    void connect({
      path: "/api/screener/run",
      method: "POST",
      body: { strategy_weights: weights, skip_ai: false, skip_selector: false },
      fetchFn: screenerFetchFn(weights),
    });
  };

  const stop = () => abort();
  const retry = () => {
    reset();
    startScreener();
  };

  // 归一化: 让权重和 = 1
  const sumWeights =
    Object.values(weights).reduce((a, b) => a + b, 0) || 1;
  const normalized = Object.fromEntries(
    Object.entries(weights).map(([k, v]) => [k, v / sumWeights]),
  );

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">
          今日粗筛
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          多策略并行 · 权重可调 · SSE 实时进度 · AI 综合评估
        </p>
      </div>

      {/* 日期 + 跑粗筛 */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 py-4">
          <div className="min-w-[180px] flex-1">
            <label className="mb-1 block text-xs text-text-secondary">
              粗筛日期(留空=最新)
            </label>
            <input
              type="text"
              list="screener-dates"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              placeholder="2026-06-25"
              className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
            />
            <datalist id="screener-dates">
              {(availableDates.data ?? []).map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
          </div>

          {running ? (
            <Button variant="ghost" onClick={stop}>
              <Square className="mr-1 h-3.5 w-3.5" />
              中断
            </Button>
          ) : status === "disconnected" ? (
            <Button variant="primary" onClick={retry}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              重试
            </Button>
          ) : (
            <Button variant="primary" onClick={startScreener} disabled={!weightsLoaded}>
              <Play className="mr-1 h-3.5 w-3.5" />
              跑粗筛
            </Button>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 策略权重 */}
        <Card>
          <CardHeader className="flex flex-row items-center gap-2 pb-2">
            <TrendingUp className="h-4 w-4 text-text-secondary" />
            <CardTitle>策略权重</CardTitle>
            <CardDescription>ED16 localStorage 持久化</CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            {strategies.isLoading ? (
              <p className="py-2 text-sm text-flat">加载策略...</p>
            ) : (
              <div className="space-y-3">
                {(strategies.data ?? []).map((s: ScreenerStrategy) => (
                  <WeightSlider
                    key={s.name}
                    strategy={s}
                    value={weights[s.name] ?? 0}
                    normalized={normalized[s.name] ?? 0}
                    onChange={(v) =>
                      setWeights((w) => ({ ...w, [s.name]: v }))
                    }
                  />
                ))}
                <p className="num pt-1 text-[11px] text-flat">
                  总权重 = {(sumWeights).toFixed(2)} · 归一化(右)
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* SSE 进度 */}
        <Card>
          <CardHeader className="flex flex-row items-center gap-2 pb-2">
            <ScanSearch className="h-4 w-4 text-text-secondary" />
            <CardTitle>粗筛进度</CardTitle>
            <CardDescription>
              {running
                ? "运行中..."
                : status === "done"
                  ? "完成"
                  : status === "disconnected"
                    ? "中断"
                    : "待启动"}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            {error && (
              <div className="mb-3 flex items-start gap-2 rounded border border-down/20 bg-down/5 p-2.5">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-down" />
                <div className="flex-1">
                  <p className="text-xs text-down">连接断开 · {error}</p>
                  <p className="mt-1 text-[10px] text-text-secondary">
                    ED8: 不自动重连(SSE 流式重发语义模糊)
                  </p>
                </div>
              </div>
            )}
            <div className="max-h-64 space-y-1 overflow-y-auto font-mono text-xs">
              {progressMessages.length === 0 && !running && (
                <p className="py-2 text-flat">点击「跑粗筛」开始</p>
              )}
              {progressMessages.map((msg, i) => (
                <div
                  key={i}
                  className="flex items-start gap-2 rounded px-2 py-1 hover:bg-bg-base"
                >
                  <CheckCircle2 className="mt-0.5 h-3 w-3 flex-shrink-0 text-up" />
                  <span className="text-text-primary">{msg}</span>
                </div>
              ))}
              {running && (
                <div className="flex items-center gap-2 px-2 py-1 text-flat">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  <span>下一步...</span>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 报告 */}
      {report && (
        <>
          {/* Summary */}
          <Card>
            <CardHeader className="flex flex-row items-center gap-2">
              <Calendar className="h-4 w-4 text-text-secondary" />
              <CardTitle>粗筛概览</CardTitle>
              <CardDescription>{report.date}</CardDescription>
            </CardHeader>
            <CardContent className="pt-0">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <SummaryItem label="扫描总数" value={String(report.summary.total_screened)} />
                <SummaryItem
                  label="通过数"
                  value={String(report.summary.passed)}
                  highlight
                />
                <SummaryItem
                  label="通过率"
                  value={formatRatio(
                    report.summary.passed / report.summary.total_screened,
                    2,
                  )}
                />
                <SummaryItem
                  label="Top picks"
                  value={String(report.picks.length)}
                />
              </div>
              {report.ai_summary && (
                <div className="mt-4 rounded-md border border-up/20 bg-up/5 p-3">
                  <div className="flex items-center gap-2">
                    <Sparkles className="h-3.5 w-3.5 text-up" />
                    <span className="text-xs font-medium text-up">AI 综合</span>
                  </div>
                  <p className="mt-1.5 text-sm">{report.ai_summary}</p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Top picks */}
          <Card>
            <CardHeader className="flex flex-row items-center gap-2">
              <Sparkles className="h-4 w-4 text-text-secondary" />
              <CardTitle>Top picks</CardTitle>
              <CardDescription>按 score 排序</CardDescription>
            </CardHeader>
            <CardContent className="pt-0">
              <div className="divide-y divide-border">
                {report.picks.map((p) => (
                  <div
                    key={p.ts_code}
                    className="flex items-center justify-between gap-3 py-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate font-medium">{p.name}</p>
                        <span className="num text-xs text-text-secondary">
                          {p.ts_code}
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-text-secondary">
                        通过 {p.passed_filters}/{p.total_filters} 过滤 ·{" "}
                        {p.notes}
                      </p>
                    </div>
                    <div className="text-right">
                      <p
                        className={cn(
                          "num font-medium",
                          p.score >= 0.7 ? "text-up" : "text-flat",
                        )}
                      >
                        {(p.score * 100).toFixed(0)}
                      </p>
                      <p className="num text-[10px] text-text-secondary">score</p>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </>
      )}

      {/* ED12 inventory 闭环: Streamlit 退役 */}
      {report && (
        <Card>
          <CardHeader className="flex flex-row items-center gap-2">
            <XCircle className="h-4 w-4 text-flat" />
            <CardTitle>Streamlit 退役</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-text-secondary">
              四 Tab(持仓/分析/回测/筛选)+ 概览首页 + 全局 chat 全部就位。
              1:1 覆盖 streamlit app.py 全部功能(ED12 inventory)。
            </p>
            <p className="mt-2 text-xs text-flat">
              后续 PR: 从 README 移除 <code className="num">streamlit run</code>{" "}
              启动说明, 删除 app.py(本 PR 验收不动, 留过渡)。
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/* ── 子组件 ──────────────────────────────────────────────── */

function WeightSlider({
  strategy,
  value,
  normalized,
  onChange,
}: {
  strategy: ScreenerStrategy;
  value: number;
  normalized: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-sm font-medium">{strategy.name}</span>
        <span className="num text-xs text-text-secondary">
          {value.toFixed(2)} · 归一 {formatRatio(normalized)}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-text-primary"
      />
      <p className="text-[11px] text-flat">{strategy.description}</p>
    </div>
  );
}

function SummaryItem({
  label,
  value,
  highlight = false,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-md border border-border p-3",
        highlight && "bg-bg-base",
      )}
    >
      <p className="text-xs text-text-secondary">{label}</p>
      <p
        className={cn(
          "num mt-1 text-lg font-semibold",
          highlight ? "text-text-primary" : "text-flat",
        )}
      >
        {value}
      </p>
    </div>
  );
}
