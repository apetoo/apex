import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Search, AlertCircle, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription, Drawer } from "@/components/base";
import { VerdictTag, VerdictDetailCard } from "@/components/a-share";
import { getAllJournal } from "@/api/analyze";
import { qk } from "@/api/query-keys";

/**
 * /journal 全部历史分析
 *
 * 跨标的查询所有 journal 记录(后端 GET /api/journal 全量倒序)。
 * 列表只放概要(date / ts_code / verdict / 置信度 / analysis_text 截 1 行);
 * 点行右侧抽屉滑出完整分析结果(复用 <VerdictDetailCard>)。
 *
 * 本地搜索: 419 条量级, 按 ts_code / verdict / analysis_text 文本过滤即可, 无需后端搜。
 */
export function JournalPage() {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);

  const journal = useQuery({
    queryKey: qk.journalAll,
    queryFn: getAllJournal,
  });

  const entries = (journal.data ?? []) as Record<string, unknown>[];

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    if (!q) return entries;
    return entries.filter((e) => {
      const hay = [
        String(e.ts_code ?? ""),
        String(e.verdict ?? ""),
        String(e.analysis_text ?? ""),
      ].join(" ").toUpperCase();
      return hay.includes(q);
    });
  }, [entries, query]);

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight">分析历史</h1>
        <p className="mt-1 text-sm text-text-secondary">
          全部标的 · 按时间倒序 · 点行查看完整分析结果
        </p>
      </div>

      {/* 搜索 */}
      <Card>
        <CardContent className="py-4">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-text-secondary" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="按 ts_code / verdict / 分析内容过滤"
              className="flex-1 bg-transparent text-sm focus:outline-none"
            />
            <span className="num text-xs text-flat">{filtered.length} 条</span>
          </div>
        </CardContent>
      </Card>

      {/* 列表 */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <History className="h-4 w-4 text-text-secondary" />
          <CardTitle>记录</CardTitle>
          <CardDescription>
            {journal.isLoading ? "加载中..." : journal.isError ? "加载失败" : `${entries.length} 条`}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          {journal.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-flat">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载中...
            </div>
          ) : journal.isError ? (
            <div className="flex items-start gap-2 rounded-md border border-down/20 bg-down/5 p-3">
              <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-down" />
              <p className="text-sm text-down">
                {journal.error instanceof Error ? journal.error.message : "加载失败"}
              </p>
            </div>
          ) : filtered.length === 0 ? (
            <p className="py-8 text-center text-sm text-flat">
              {entries.length === 0 ? "暂无历史分析" : "无匹配记录"}
            </p>
          ) : (
            <div className="divide-y divide-border">
              {filtered.map((e, i) => {
                const pa = (e.price_advice ?? {}) as Record<string, unknown>;
                const at = String(e.analyzed_at ?? e.date ?? "");
                const text = String(e.analysis_text ?? "");
                return (
                  <button
                    key={`${e.ts_code}-${at}-${i}`}
                    type="button"
                    onClick={() => setSelected(e)}
                    className="flex w-full items-start justify-between gap-3 py-3 text-left hover:bg-bg-base"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="num text-sm font-medium text-text-primary">
                          {String(e.ts_code ?? "")}
                        </span>
                        <span className="num text-[11px] text-flat">{at}</span>
                      </div>
                      <p className="mt-0.5 line-clamp-1 text-xs text-text-secondary">
                        {text || "—"}
                      </p>
                      <p className="num mt-0.5 text-[11px] text-flat">
                        入场 {fmtOrDash(pa.entry)} · 止损 {fmtOrDash(pa.stop_loss)} · 目标 {fmtOrDash(pa.target)}
                      </p>
                    </div>
                    <div className="flex flex-col items-end gap-0.5">
                      <VerdictTag verdict={String(e.verdict ?? "")} />
                      <span className="num text-[11px] text-text-secondary">
                        置信度 {String(e.confidence ?? "—")}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 详情抽屉 */}
      <Drawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={
          selected ? (
            <span className="flex items-center gap-2">
              <span className="num">{String(selected.ts_code ?? "")}</span>
              <span className="num text-xs text-flat">
                {String(selected.analyzed_at ?? selected.date ?? "")}
              </span>
            </span>
          ) : null
        }
        widthClass="max-w-lg"
      >
        {selected && <VerdictDetailCard verdict={selected} />}
      </Drawer>
    </div>
  );
}

function fmtOrDash(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : String(v);
}
