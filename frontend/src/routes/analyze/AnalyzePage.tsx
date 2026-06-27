import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Search } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/base";
import { AnalyzeTraceStream, VerdictDetailCard, VerdictTag, PriceTag } from "@/components/a-share";
import { getJournal } from "@/api/analyze";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { useChatContext } from "@/hooks/useChatContext";

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
 */

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
  const [tsCode, setTsCode] = useState("");
  const [committedCode, setCommittedCode] = useState<string | null>(null);
  const [latestVerdict, setLatestVerdict] = useState<Record<string, unknown> | null>(null);

  const { setContext } = useChatContext();

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
              <CardTitle className="text-sm font-normal text-text-secondary">
                {committedCode}
              </CardTitle>
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
                  return (
                    <div key={i} className="flex items-center justify-between py-3">
                      <div className="min-w-0">
                        <p className="num text-xs text-text-secondary">
                          {String(e.analyzed_at ?? "")}
                        </p>
                        <p className="mt-0.5 truncate text-sm">
                          {String(e.note ?? e.analysis_text ?? "")}
                        </p>
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
