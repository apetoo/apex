import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Search, ChevronRight, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/base";
import { AnalyzeTraceStream, VerdictDetailCard, VerdictTag, PriceTag, TraceEventList } from "@/components/a-share";
import { getJournal, getTrace } from "@/api/analyze";
import { getPrices, getDailyPrices, getStockInfo } from "@/api/market";
import { qk } from "@/api/query-keys";
import { useChatContext } from "@/hooks/useChatContext";
import { cn } from "@/lib/utils";

/**
 * /analyze 个股分析页
 *
 * 流程: 输入 ts_code → (防抖)自动 commit → 自动跑 AI 分析(trace 流式)
 *       → 展示 verdict(VerdictDetailCard) → 写入 journal(后端 save=true) → 历史 journal 列表
 *
 * UX:
 *   - **无按钮**: 输入代码停顿即自动分析(回车立即开始)。原流程要点两次按钮, 不合理。
 *   - **结果用 VerdictDetailCard**: 正确读 price_advice 嵌套 + analysis_text(markdown)。
 *   - **过程默认折叠**: 见 <AnalyzeTraceStream>。
 *
 * ED1: useSSE hook, 都关自动重连, 断流手动重试
 * ED2: setContext(verdict 摘要) → chat 呼出时自动注入
 *
 * 持久化: tsCode/committedCode/latestVerdict 存 sessionStorage, 切走再回不丢「已分析的结果」。
 *   - 仅恢复本会话展示过的 verdict, 不主动从 journal 拉历史(避免首次进入就显示旧 verdict)。
 *   - SSE 随路由 unmount 被 useSSE abort, 中途切走的不完整分析不恢复(只恢复已 done 的 verdict)。
 */

const ANALYZE_STATE_KEY = "apex.analyze.state";

interface PersistedAnalyzeState {
  tsCode: string;
  committedCode: string | null;
  latestVerdict: Record<string, unknown> | null;
}

function loadPersistedState(): PersistedAnalyzeState | null {
  try {
    const raw = sessionStorage.getItem(ANALYZE_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedAnalyzeState;
    if (typeof parsed.tsCode !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

/** 前端 ts_code 归一化(镜像后端 data.normalize_ts_code: 6→SH, 0/3→SZ, 4/8→BJ)。非法返回 null。 */
function normalizeTsCode(raw: string): string | null {
  const s = raw.trim().toUpperCase();
  if (!/^\d{6}(\.(SH|SZ|BJ))?$/.test(s)) return null;
  if (s.length === 6) {
    const d = s[0];
    const suf = d === "6" ? "SH" : d === "0" || d === "3" ? "SZ" : d === "4" || d === "8" ? "BJ" : null;
    return suf ? `${s}.${suf}` : null;
  }
  return s;
}

export function AnalyzePage() {
  // 仅 mount 时读一次 sessionStorage(切回页面时拿最新持久化值)。
  const persisted = useMemo(() => loadPersistedState(), []);
  const [tsCode, setTsCode] = useState(persisted?.tsCode ?? "");
  const [committedCode, setCommittedCode] = useState<string | null>(persisted?.committedCode ?? null);
  const [latestVerdict, setLatestVerdict] = useState<Record<string, unknown> | null>(persisted?.latestVerdict ?? null);
  // 历史 journal 行展开回放：存当前展开的 analyzed_at（null=全收起）
  const [expandedAt, setExpandedAt] = useState<string | null>(null);

  const { setContext } = useChatContext();

  // 持久化: 三者任一变化即写 sessionStorage, 切走再回可恢复。
  useEffect(() => {
    const state: PersistedAnalyzeState = { tsCode, committedCode, latestVerdict };
    try {
      sessionStorage.setItem(ANALYZE_STATE_KEY, JSON.stringify(state));
    } catch {
      // quota / 隐私模式 —— 忽略, 持久化是 best-effort
    }
  }, [tsCode, committedCode, latestVerdict]);

  // 防抖自动 commit: 输入停顿 500ms 且归一化结果变化才 commit(避免补后缀时抖动)。
  // commit 只决定「下方内容」(行情/历史/分析区占位)是否展示, 不触发 AI 分析 ——
  // 分析需点 <AnalyzeTraceStream> 头部的「开始分析」按钮。
  useEffect(() => {
    const norm = normalizeTsCode(tsCode);
    if (!norm || norm === committedCode) return;
    const t = setTimeout(() => {
      setCommittedCode(norm);
      setLatestVerdict(null);
    }, 500);
    return () => clearTimeout(t);
  }, [tsCode, committedCode]);

  // 跑分析时, 实时价 + 昨收(给分析面板旁边显示, 也让 chat 上下文带)
  const prices = useQuery({
    queryKey: qk.prices(committedCode ? [committedCode] : []),
    queryFn: () => getPrices([committedCode!]),
    enabled: !!committedCode,
  });
  const daily = useQuery({
    queryKey: qk.dailyPrices(committedCode ? [committedCode] : []),
    queryFn: () => getDailyPrices([committedCode!]),
    enabled: !!committedCode,
  });
  // 股票名称(展示用, 失败/mock 未命中降级只显示代码)
  const stockInfo = useQuery({
    queryKey: qk.stockInfo(committedCode ?? ""),
    queryFn: () => getStockInfo(committedCode!),
    enabled: !!committedCode,
    staleTime: Infinity, // 名称不变, 永不重拉
  });
  const stockName = stockInfo.data?.name;

  // 历史 journal
  const journal = useQuery({
    queryKey: qk.journal(committedCode ?? ""),
    queryFn: () => getJournal(committedCode!),
    enabled: !!committedCode,
  });

  // 把当前分析的 verdict 摘要注入 chat 上下文(ED2)
  useEffect(() => {
    if (latestVerdict && committedCode) {
      const pa = (latestVerdict.price_advice ?? {}) as Record<string, unknown>;
      const summary = [
        `标的: ${committedCode}`,
        `最新 verdict: ${latestVerdict.verdict ?? "—"} (置信度 ${latestVerdict.confidence ?? "—"})`,
        `入场: ${pa.entry ?? "—"} / 止损: ${pa.stop_loss ?? "—"} / 目标: ${pa.target ?? "—"}`,
      ].join("\n");
      setContext(summary);
    }
  }, [latestVerdict, committedCode, setContext]);

  const currentPrice = prices.data?.[committedCode ?? ""] ?? null;
  const prevClose = daily.data?.[committedCode ?? ""] ?? null;

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">个股分析</h1>
        <p className="mt-1 text-sm text-text-secondary">
          输入代码自动分析 · 过程可展开 · 结果自动注入 chat 上下文
        </p>
      </div>

      {/* 输入区: 仅展示下方内容, 不自动分析 —— 点分析卡里的「开始分析」才跑 */}
      <Card>
        <CardContent className="flex items-center gap-2 py-4">
          <Search className="h-4 w-4 flex-shrink-0 text-flat" />
          <input
            type="text"
            value={tsCode}
            onChange={(e) => setTsCode(e.target.value.toUpperCase())}
            placeholder="输入代码, e.g. 000001 或 000001.SZ"
            autoFocus
            className="num flex-1 rounded-md border border-border bg-bg-card px-3 py-2 text-sm focus:border-text-secondary focus:outline-none"
          />
          <span className="hidden text-xs text-flat sm:inline">
            输入代码展示下方信息 · 点「开始分析」跑 AI
          </span>
        </CardContent>
      </Card>

      {committedCode && (
        <div className="grid gap-4 lg:grid-cols-3">
          {/* 左: 行情速览 */}
          <Card className="lg:col-span-1">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-baseline gap-2 text-sm font-normal text-text-secondary">
                <span className="num">{committedCode}</span>
                {stockName ? (
                  <span className="text-text-primary">{stockName}</span>
                ) : stockInfo.isLoading ? (
                  <span className="text-flat">…</span>
                ) : null}
              </CardTitle>
              {/* 行业 · 市场: /info 已返回的字段, AI 常按行业归类, 市场定涨跌停规则(主板±10%/创科±20%/北交所±30%) */}
              {(stockInfo.data?.industry || stockInfo.data?.market) && (
                <p className="text-xs text-flat">
                  {[stockInfo.data?.industry, stockInfo.data?.market]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              )}
            </CardHeader>
            <CardContent>
              <PriceTag
                price={currentPrice}
                prevClose={prevClose}
                loading={prices.isLoading || daily.isLoading}
                error={prices.isError || daily.isError}
                size="lg"
              />
            </CardContent>
          </Card>

          {/* 右: 分析结果(VerdictDetailCard) / 未分析占位 */}
          <div className="space-y-4 lg:col-span-2">
            {latestVerdict ? (
              <VerdictDetailCard verdict={latestVerdict} />
            ) : (
              <Card>
                <CardContent className="flex items-center gap-3 py-10">
                  <p className="text-sm text-text-secondary">
                    点下方「开始分析」跑 AI 工具链
                    <span className="text-flat">（过程可展开查看）</span>
                  </p>
                </CardContent>
              </Card>
            )}

            {/* 分析过程(默认折叠) */}
            <AnalyzeTraceStream
              key={committedCode}
              tsCode={committedCode}
              onVerdict={setLatestVerdict}
            />
          </div>
        </div>
      )}

      {/* 历史 journal */}
      {committedCode && (
        <Card>
          <CardHeader className="flex flex-row items-center gap-2">
            <History className="h-4 w-4 text-text-secondary" />
            <CardTitle>历史分析</CardTitle>
            <CardDescription>
              {journal.data ? `${journal.data.length} 条` : "加载中..."}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            {journal.isLoading ? (
              <p className="py-4 text-center text-sm text-flat">加载中...</p>
            ) : journal.data && journal.data.length > 0 ? (
              <div className="divide-y divide-border">
                {journal.data.map((entry: unknown, i: number) => {
                  const e = entry as Record<string, unknown>;
                  const pa = (e.price_advice ?? {}) as Record<string, unknown>;
                  const at = String(e.analyzed_at ?? "");
                  const isOpen = expandedAt === at;
                  return (
                    <div key={i}>
                      <button
                        type="button"
                        onClick={() => setExpandedAt(isOpen ? null : at)}
                        className="flex w-full items-center justify-between py-3 text-left hover:bg-bg-base"
                      >
                        <div className="min-w-0 flex items-center gap-1.5">
                          <ChevronRight
                            className={cn(
                              "h-3.5 w-3.5 flex-shrink-0 text-flat transition-transform",
                              isOpen && "rotate-90",
                            )}
                          />
                          <div className="min-w-0">
                            <p className="num text-xs text-text-secondary">{at}</p>
                            <p className="mt-0.5 truncate text-sm">
                              {String(e.note ?? e.analysis_text ?? "")}
                            </p>
                          </div>
                        </div>
                        <div className="ml-3 flex-shrink-0 text-right">
                          <VerdictTag verdict={String(e.verdict ?? "")} />
                          <p className="num mt-0.5 text-xs text-text-secondary">
                            置信度 {String(e.confidence ?? "—")}
                          </p>
                          {pa.entry !== undefined && (
                            <p className="num text-[11px] text-flat">
                              入 {String(pa.entry)} · 止 {String(pa.stop_loss)} · 目 {String(pa.target)}
                            </p>
                          )}
                        </div>
                      </button>
                      {isOpen && (
                        <HistoryTraceReplay tsCode={committedCode!} analyzedAt={at} />
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="py-4 text-center text-sm text-flat">暂无历史</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** 历史行展开的 trace 回放块：按 (ts_code, analyzed_at) 拉 trace.jsonl。null 降级。 */
function HistoryTraceReplay({ tsCode, analyzedAt }: { tsCode: string; analyzedAt: string }) {
  const trace = useQuery({
    queryKey: qk.trace(tsCode, analyzedAt),
    queryFn: () => getTrace(tsCode, analyzedAt),
    enabled: !!tsCode && !!analyzedAt,
  });

  if (trace.isLoading) {
    return (
      <p className="py-3 text-center text-xs text-flat">
        <Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />
        加载过程…
      </p>
    );
  }
  if (trace.data?.events?.length) {
    return (
      <div className="pb-3 pl-5">
        <TraceEventList events={trace.data.events} />
      </div>
    );
  }
  return (
    <p className="py-3 pl-5 text-xs text-flat">无过程记录（早期分析未落 trace）</p>
  );
}
