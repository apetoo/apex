import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Calendar, Plus, X } from "lucide-react";
import { Card, CardContent, CardHeader, Button } from "@/components/base";
import { IntradayChart } from "@/components/a-share";
import { getStockInfo, type IntradayBars } from "@/api/market";
import { qk } from "@/api/query-keys";
import { SECTOR_PRESETS, SECTOR_ORDER, ETF_NAMES } from "./sectors";
import {
  cn,
  directionClass,
  formatPercent,
  formatPrice,
  normalizeTsCode,
} from "@/lib/utils";

/**
 * /intraday 分时看板
 *
 * 灵活的多股分时看板: 选日期(默认当日) + 选板块预设 或 手动输入股票代码,
 * 网格展示每只股票的当日/历史分时图。板块仅是一组快捷代码, 不写死。
 *
 * 数据源: /api/market/intraday/{ts_code}/bars?trade_date=YYYYMMDD
 *   - 后端 akshare stock_zh_a_minute 仅返回最近约 5-8 个交易日, 更早 fail-soft 空图
 *   - 卡片头的「最新价/涨跌幅」由 IntradayChart 的 onDataLoaded 回传, 不重复拉 bars
 *
 * 持久化: date/sector/codes 存 localStorage, 刷新不丢(ED16 同款)。
 */

const LS_KEY = "apex.intraday.state";

interface BoardState {
  date: string; // YYYY-MM-DD (<input type=date> value)
  sector: string | null;
  codes: string; // textarea 草稿
}

function todayStr(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function loadState(): BoardState | null {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as BoardState;
    if (typeof p.date !== "string") return null;
    return p;
  } catch {
    return null;
  }
}

function saveState(s: BoardState) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(s));
  } catch {
    // ignore
  }
}

/** 解析 textarea 草稿: 逗号/空格/换行/中文逗号/顿号分隔 -> 归一化去重 */
function parseCodes(raw: string): { valid: string[]; invalid: string[] } {
  const tokens = raw.split(/[\s,，、]+/).map((t) => t.trim()).filter(Boolean);
  const valid: string[] = [];
  const invalid: string[] = [];
  const seen = new Set<string>();
  for (const t of tokens) {
    const norm = normalizeTsCode(t);
    if (!norm) {
      invalid.push(t);
      continue;
    }
    if (seen.has(norm)) continue;
    seen.add(norm);
    valid.push(norm);
  }
  return { valid, invalid };
}

const toTradeDate = (dateStr: string) => dateStr.replace(/-/g, ""); // YYYY-MM-DD -> YYYYMMDD

export function IntradayBoardPage() {
  const persisted = useMemo(() => loadState(), []);
  const [date, setDate] = useState(persisted?.date ?? todayStr());
  const [sector, setSector] = useState<string | null>(persisted?.sector ?? null);
  const [codesText, setCodesText] = useState(persisted?.codes ?? "");
  // applied = 已应用渲染的代码列表(点板块或「应用」后同步); codesText 是草稿
  const [applied, setApplied] = useState<string[]>(() =>
    parseCodes(persisted?.codes ?? "").valid,
  );

  useEffect(() => {
    saveState({ date, sector, codes: codesText });
  }, [date, sector, codesText]);

  const tradeDate = toTradeDate(date);
  const { valid, invalid } = useMemo(() => parseCodes(codesText), [codesText]);

  function applySector(name: string) {
    const codes = (SECTOR_PRESETS[name] ?? []).map((c) => normalizeTsCode(c)!).filter(Boolean);
    setSector(name);
    setCodesText((SECTOR_PRESETS[name] ?? []).join(", "));
    setApplied(codes);
  }

  function applyManual() {
    setSector(null);
    setApplied(valid);
  }

  function applyAllSectors() {
    const all = SECTOR_ORDER.flatMap((n) => SECTOR_PRESETS[n] ?? [])
      .map((c) => normalizeTsCode(c)!)
      .filter(Boolean);
    setSector(null);
    setCodesText(all.join(", "));
    setApplied(all);
  }

  function removeCode(code: string) {
    const next = applied.filter((c) => c !== code);
    setApplied(next);
    setCodesText(next.join(", "));
    setSector(null);
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      {/* 控制栏 */}
      <Card className="mb-6">
        <CardContent className="pt-5">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <Calendar className="h-4 w-4" />
              日期
              <input
                type="date"
                value={date}
                max={todayStr()}
                onChange={(e) => setDate(e.target.value)}
                className="rounded-md border border-border bg-bg-card px-2 py-1 text-sm text-text-primary"
              />
            </label>
            <span className="text-xs text-text-secondary">
              仅最近约 5-8 个交易日可查
            </span>
            <div className="ml-auto flex flex-wrap gap-1.5">
              {SECTOR_ORDER.map((name) => (
                <Button
                  key={name}
                  size="sm"
                  variant={sector === name ? "default" : "outline"}
                  onClick={() => applySector(name)}
                >
                  {name}
                </Button>
              ))}
              <Button
                size="sm"
                variant="ghost"
                onClick={applyAllSectors}
                title="一键加载全部 7 个板块 ETF"
              >
                全部
              </Button>
            </div>
          </div>

          <div className="mt-4 flex gap-2">
            <textarea
              value={codesText}
              onChange={(e) => {
                setCodesText(e.target.value);
                setSector(null);
              }}
              placeholder="输入股票代码，逗号/空格/换行分隔，如 601318, 600276, 300308"
              rows={2}
              className="flex-1 resize-y rounded-md border border-border bg-bg-card px-3 py-2 text-sm text-text-primary placeholder:text-text-secondary/60"
            />
            <Button variant="primary" onClick={applyManual} className="self-stretch">
              <Plus className="h-4 w-4" />
              应用
            </Button>
          </div>
          {invalid.length > 0 && (
            <div className="mt-2 text-xs text-down">
              无法识别的代码: {invalid.join(", ")}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 卡片网格 */}
      {applied.length === 0 ? (
        <div className="py-16 text-center text-sm text-text-secondary">
          选择一个板块预设，或输入股票代码后点「应用」
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {applied.map((code) => (
            <BoardCard
              key={code + "@" + tradeDate}
              tsCode={code}
              tradeDate={tradeDate}
              onRemove={() => removeCode(code)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface BoardCardProps {
  tsCode: string;
  tradeDate: string;
  onRemove: () => void;
}

interface CardSummary {
  last?: number;
  pct?: number;
  tradeDate?: string;
  empty?: boolean;
}

function BoardCard({ tsCode, tradeDate, onRemove }: BoardCardProps) {
  // 名称(走 react-query 缓存, 失败降级只显代码)
  const info = useQuery({
    queryKey: qk.stockInfo(tsCode),
    queryFn: () => getStockInfo(tsCode),
    staleTime: Infinity,
    retry: 1,
  });

  // 最新价/涨跌幅由 IntradayChart onDataLoaded 回传, 避免重复拉 bars
  const [summary, setSummary] = useState<CardSummary | null>(null);

  function onDataLoaded(data: IntradayBars) {
    const bars = data.bars ?? [];
    if (bars.length === 0) {
      setSummary({ empty: true, tradeDate: data.trade_date });
      return;
    }
    const last = bars[bars.length - 1].close;
    const prev = data.prev_close;
    const pct = prev && prev > 0 ? ((last - prev) / prev) * 100 : undefined;
    setSummary({ last, pct, tradeDate: data.trade_date });
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-2">
            <span className="font-medium text-text-primary">
              {ETF_NAMES[tsCode] ?? info.data?.name ?? tsCode}
            </span>
            <span className="text-xs text-text-secondary">{tsCode}</span>
          </div>
          <button
            onClick={onRemove}
            className="text-text-secondary transition-colors hover:text-down"
            title="移除"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex items-baseline gap-2">
          {summary?.empty ? (
            <span className="text-xs text-text-secondary">
              该日无分时数据（非交易日 / 超出可查范围）
            </span>
          ) : (
            <>
              {summary?.last != null && (
                <span className={cn("num text-lg font-medium", directionClass(summary.pct ?? 0))}>
                  {formatPrice(summary.last)}
                </span>
              )}
              {summary?.pct != null && (
                <span className={cn("num text-sm", directionClass(summary.pct))}>
                  {formatPercent(summary.pct)}
                </span>
              )}
              {summary == null && (
                <span className="text-xs text-text-secondary">加载中…</span>
              )}
            </>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <IntradayChart tsCode={tsCode} tradeDate={tradeDate} height={220} onDataLoaded={onDataLoaded} />
      </CardContent>
    </Card>
  );
}
