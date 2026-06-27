import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Sparkles } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription, Button } from "@/components/base";
import { AnalyzeTraceStream, VerdictTag, PriceTag } from "@/components/a-share";
import { getJournal } from "@/api/analyze";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { useChatContext } from "@/hooks/useChatContext";
import { formatPrice } from "@/lib/utils";

/**
 * /analyze 个股分析页
 *
 * 流程: 选 ts_code → 跑 AI 分析(trace 流式) → 展示 verdict
 *       → 写入 journal(后端 save=true) → 历史 journal 列表
 *
 * ED1: useSSE hook, 都关自动重连, 断流手动重试
 * ED2: setContext(verdict 摘要) → chat 呼出时自动注入
 */
export function AnalyzePage() {
  const [tsCode, setTsCode] = useState("");
  const [committedCode, setCommittedCode] = useState<string | null>(null);
  const [latestVerdict, setLatestVerdict] = useState<Record<string, unknown> | null>(null);

  const canRun = tsCode.trim().length > 0;

  const { setContext } = useChatContext();

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
      const summary = [
        `标的: ${committedCode}`,
        `最新 verdict: ${latestVerdict.verdict ?? "—"} (置信度 ${latestVerdict.confidence ?? "—"})`,
        `入场: ${latestVerdict.entry_price ?? "—"} / 止损: ${latestVerdict.stop_loss ?? "—"} / 目标: ${latestVerdict.target ?? "—"}`,
        `regime: ${latestVerdict.regime ?? "—"}`,
      ].join("\n");
      setContext(summary);
    }
  }, [latestVerdict, committedCode, setContext]);

  const currentPrice = prices.data?.[committedCode ?? ""] ?? null;
  const prevClose = daily.data?.[committedCode ?? ""] ?? null;

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">
          个股分析
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          选 ts_code · AI trace 流式 · verdict 自动注入 chat 上下文
        </p>
      </div>

      {/* 输入区 */}
      <Card>
        <CardContent className="flex items-center gap-2 py-4">
          <input
            type="text"
            value={tsCode}
            onChange={(e) => setTsCode(e.target.value.toUpperCase())}
            placeholder="000001.SZ"
            className="num flex-1 rounded-md border border-border bg-bg-card px-3 py-2 text-sm focus:border-text-secondary focus:outline-none"
          />
          <Button
            variant="primary"
            disabled={!canRun}
            onClick={() => {
              setCommittedCode(tsCode);
              setLatestVerdict(null);
            }}
          >
            <Sparkles className="mr-1 h-3.5 w-3.5" />
            跑分析
          </Button>
        </CardContent>
      </Card>

      {/* 行情 + trace 联调 */}
      {committedCode && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card>
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
              {latestVerdict && (
                <div className="mt-3 space-y-1.5 border-t border-border pt-3">
                  <div className="flex items-center gap-2">
                    <VerdictTag verdict={String(latestVerdict.verdict ?? "")} />
                    <span className="num text-xs text-text-secondary">
                      置信度 {String(latestVerdict.confidence)}
                    </span>
                  </div>
                  <p className="num text-xs text-text-secondary">
                    入场 {formatPrice(Number(latestVerdict.entry_price))} · 止损{" "}
                    {formatPrice(Number(latestVerdict.stop_loss))} · 目标{" "}
                    {formatPrice(Number(latestVerdict.target))}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <div className="lg:col-span-2">
            <AnalyzeTraceStream
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
                  return (
                    <div
                      key={i}
                      className="flex items-center justify-between py-3"
                    >
                      <div>
                        <p className="num text-xs text-text-secondary">
                          {String(e.analyzed_at ?? "")}
                        </p>
                        <p className="mt-0.5 text-sm">
                          {String(e.note ?? "")}
                        </p>
                      </div>
                      <div className="text-right">
                        <VerdictTag verdict={String(e.verdict ?? "")} />
                        <p className="num mt-0.5 text-xs text-text-secondary">
                          置信度 {String(e.confidence ?? "—")}
                        </p>
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
