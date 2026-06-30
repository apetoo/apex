import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw, Wallet, Bell, History, ListTree } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
  Dialog,
} from "@/components/base";
import { CompactPositionCard } from "@/components/a-share/CompactPositionCard";
import { CompactCandidateCard } from "@/components/a-share/CompactCandidateCard";
import { getPrices, getDailyPrices } from "@/api/market";
import { getLatestJournal } from "@/api/analyze";
import {
  getWatchlist,
  getTrades,
  type ActivePosition,
  type Candidate,
} from "@/api/watchlist";
import {
  useAddCandidate,
  useBuy,
  useSell,
  useClosePosition,
  useArchiveEntry,
  usePromoteCandidate,
  useUpdateAdvice,
} from "@/api/mutations";
import { qk } from "@/api/query-keys";
import { formatPrice } from "@/lib/utils";

/**
 * /watchlist 持仓 & 候选页
 *
 * 借鉴 21st.dev PortfolioCard 结构(divide-y 列表 + 多 section 卡片),
 * 翻成 A 股红涨绿跌。
 *
 * 1:1 对应 Streamlit tab_wl 功能(ED12 inventory):
 *   - 账户配置(顶部 expander,PR1b 占位)
 *   - 刷新价格(manual, ED4)
 *   - 持仓列表(active_positions)
 *   - 候选列表(candidates)
 *   - 增删候选 / promote / 平仓 / 归档(占位按钮,后续 PR 接 mutation)
 *   - 触发器信号(PR1b 暂用候选,PR1b+ 接 /api/triggers)
 */
export function WatchlistPage() {
  const navigate = useNavigate();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: qk.watchlist,
    queryFn: getWatchlist,
  });

  // mutations(ED3 失效矩阵接通)
  const addCandidateMut = useAddCandidate();
  const buyMut = useBuy();
  const sellMut = useSell();
  const closeMut = useClosePosition();
  const archiveMut = useArchiveEntry();
  const promoteMut = usePromoteCandidate();
  const adviceMut = useUpdateAdvice();
  // 正在同步 AI advice 的 ts_code(单只反馈, 避免全局 spinner)
  const [syncingCode, setSyncingCode] = useState<string | null>(null);

  const { data: tradesData } = useQuery({
    queryKey: qk.trades,
    queryFn: () => getTrades({ limit: 50 }),
  });

  // 开仓表单
  const [showOpen, setShowOpen] = useState(false);
  const [openCode, setOpenCode] = useState("");
  const [openPrice, setOpenPrice] = useState("");
  const [openShares, setOpenShares] = useState("");
  const [openStop, setOpenStop] = useState("");
  const [openTarget, setOpenTarget] = useState("");
  const [openNote, setOpenNote] = useState("");

  // 加仓/减仓目标 + 表单
  const [tradeTarget, setTradeTarget] = useState<ActivePosition | null>(null);
  const [tradeSide, setTradeSide] = useState<"buy" | "sell">("buy");
  const [tradePrice, setTradePrice] = useState("");
  const [tradeShares, setTradeShares] = useState("");
  const [tradeNote, setTradeNote] = useState("");

  // 加候选 简单 prompt
  const [addCode, setAddCode] = useState("");
  const [addPrice, setAddPrice] = useState("");
  const [showAdd, setShowAdd] = useState(false);

  // 平仓弹窗
  const [closeTarget, setCloseTarget] = useState<ActivePosition | null>(null);
  const [closePrice, setClosePrice] = useState("");
  const [closeDate, setCloseDate] = useState("");
  const [closeNote, setCloseNote] = useState("");
  const [closeDiag, setCloseDiag] = useState<string | null>(null);

  // promote(候选→持仓)弹窗
  const [promoteTarget, setPromoteTarget] = useState<Candidate | null>(null);
  const [promotePrice, setPromotePrice] = useState("");
  const [promoteShares, setPromoteShares] = useState("");
  const [promoteStop, setPromoteStop] = useState("");
  const [promoteTargetPrice, setPromoteTargetPrice] = useState("");

  const submitAdd = (e?: React.FormEvent) => {
    e?.preventDefault();
    const tsCode = addCode.trim().toUpperCase();
    const price = Number(addPrice);
    if (!tsCode || !price) return;
    addCandidateMut.mutate(
      {
        ts_code: tsCode,
        // 传空让后端 _resolve_name 查 tushare 补真名
        name: "",
        trigger_price: price,
        note: `手动加候选 触发 ${price}`,
      },
      {
        onSuccess: () => {
          setAddCode("");
          setAddPrice("");
          setShowAdd(false);
        },
      },
    );
  };

  const submitOpen = (e?: React.FormEvent) => {
    e?.preventDefault();
    const tsCode = openCode.trim().toUpperCase();
    const price = Number(openPrice);
    const shares = Number(openShares);
    if (!tsCode || !price || !shares) return;
    buyMut.mutate(
      {
        ts_code: tsCode,
        fill_price: price,
        shares,
        stop_loss: openStop ? Number(openStop) : undefined,
        target: openTarget ? Number(openTarget) : undefined,
        note: openNote,
      },
      {
        onSuccess: () => {
          setShowOpen(false);
          setOpenCode(""); setOpenPrice(""); setOpenShares("");
          setOpenStop(""); setOpenTarget(""); setOpenNote("");
        },
      },
    );
  };

  const openTradeForm = (p: ActivePosition, side: "buy" | "sell") => {
    setTradeTarget(p);
    setTradeSide(side);
    setTradePrice("");
    setTradeShares("");
    setTradeNote("");
  };

  const submitTrade = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!tradeTarget) return;
    const price = Number(tradePrice);
    const shares = Number(tradeShares);
    if (!price || !shares) return;
    const payload = {
      ts_code: tradeTarget.ts_code,
      fill_price: price,
      shares,
      note: tradeNote,
    };
    const mut = tradeSide === "buy" ? buyMut : sellMut;
    mut.mutate(payload, {
      onSuccess: () => setTradeTarget(null),
    });
  };

  // —— 平仓 —— 打开弹窗时预填当前价 + 今天日期
  const openCloseForm = async (p: ActivePosition) => {
    setCloseTarget(p);
    setClosePrice("");
    setCloseNote("");
    setCloseDiag(null);
    setCloseDate(new Date().toISOString().slice(0, 10));
    // 预填当前价: 实时价优先, 回退日收盘
    try {
      const [rt, daily] = await Promise.all([
        getPrices([p.ts_code]),
        getDailyPrices([p.ts_code]),
      ]);
      const cur = rt?.[p.ts_code] ?? daily?.[p.ts_code] ?? null;
      if (cur != null) setClosePrice(String(cur));
    } catch {
      /* 拿不到就让用户手填 */
    }
  };

  const submitClose = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!closeTarget) return;
    const price = Number(closePrice);
    if (!price || !closeDate) return;
    closeMut.mutate(
      {
        ts_code: closeTarget.ts_code,
        exit_price: price,
        exit_date: closeDate,
        user_notes: closeNote || undefined,
        // postmortem 后端默认 true, 跑 AI 复盘 + 校准
      },
      {
        onSuccess: (resp) => {
          setCloseDiag(resp.diagnosis ?? null);
          // 保留弹窗展示 diagnosis, 但清掉 target 防止重复提交;
          // 用户看完 diagnosis 点关闭清场
          setCloseTarget(null);
        },
      },
    );
  };

  // —— 候选归档 —— 直接调, 软删除(reason=manual)
  const handleArchive = (c: Candidate) => {
    if (!window.confirm(`归档候选 ${c.name || c.ts_code}? (软删除, 可在归档区找回)`))
      return;
    archiveMut.mutate({ ts_code: c.ts_code, reason: "manual", section: "candidates" });
  };

  // —— promote 候选→持仓 —— 打开弹窗预填候选 trigger_price / stop_advice / target_advice
  const openPromoteForm = (c: Candidate) => {
    setPromoteTarget(c);
    setPromotePrice(c.trigger_price ? String(c.trigger_price) : "");
    setPromoteShares("100");
    setPromoteStop(c.stop_advice ? String(c.stop_advice) : "");
    setPromoteTargetPrice(c.target_advice ? String(c.target_advice) : "");
  };

  const submitPromote = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!promoteTarget) return;
    const entry = Number(promotePrice);
    const shares = Number(promoteShares);
    const stop = Number(promoteStop);
    const target = Number(promoteTargetPrice);
    if (!entry || !shares || !stop || !target) return;
    promoteMut.mutate(
      { ts_code: promoteTarget.ts_code, entry_price: entry, shares, stop_loss: stop, target },
      { onSuccess: () => setPromoteTarget(null) },
    );
  };

  // —— 同步 AI advice —— 拉最近一次 AI 分析, 用其 price_advice 覆盖持仓 stop_loss/target
  const handleSyncAi = async (p: ActivePosition) => {
    setSyncingCode(p.ts_code);
    try {
      const latest = await getLatestJournal(p.ts_code);
      if (!latest || !latest.price_advice) {
        window.alert(`${p.name || p.ts_code} 暂无 AI 分析记录, 无法同步`);
        return;
      }
      const pa = latest.price_advice;
      const stop = pa.stop_loss ?? null;
      const target = pa.target ?? null;
      if (stop == null && target == null) {
        window.alert(`${p.name || p.ts_code} 最近 AI 分析未给出止损/目标`);
        return;
      }
      adviceMut.mutate(
        {
          ts_code: p.ts_code,
          stop_loss: stop ?? undefined,
          target: target ?? undefined,
          calibrated_confidence: latest.calibrated_confidence,
        },
        {
          onError: (e) => window.alert(`同步失败 · ${String(e)}`),
        },
      );
    } catch (e) {
      window.alert(`拉取 AI 分析失败 · ${String(e)}`);
    } finally {
      setSyncingCode(null);
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="font-serif text-3xl font-semibold tracking-tight">
            持仓 & 候选
          </h1>
          <p className="mt-1 text-sm text-text-secondary">
            真实持仓 · 触发候选 · 平仓复盘
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refetch()}
            disabled={isLoading}
          >
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
            刷新
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => setShowOpen((s) => !s)}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            加持仓
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowAdd((s) => !s)}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            加候选
          </Button>
        </div>
      </div>

      {showOpen && (
        <Card>
          <CardContent className="py-4">
            <form onSubmit={submitOpen} className="flex flex-wrap items-end gap-3">
              <div className="min-w-[140px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">代码 *</label>
                <input type="text" value={openCode} onChange={(e) => setOpenCode(e.target.value)}
                  placeholder="000001.SZ"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[100px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">成本价 *</label>
                <input type="number" step="0.01" value={openPrice} onChange={(e) => setOpenPrice(e.target.value)}
                  placeholder="12.50"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[100px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">股数 *</label>
                <input type="number" step="100" value={openShares} onChange={(e) => setOpenShares(e.target.value)}
                  placeholder="1000"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[90px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">止损</label>
                <input type="number" step="0.01" value={openStop} onChange={(e) => setOpenStop(e.target.value)}
                  placeholder="可选"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[90px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">目标</label>
                <input type="number" step="0.01" value={openTarget} onChange={(e) => setOpenTarget(e.target.value)}
                  placeholder="可选"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[140px] flex-[2]">
                <label className="mb-1 block text-xs text-text-secondary">备注</label>
                <input type="text" value={openNote} onChange={(e) => setOpenNote(e.target.value)}
                  placeholder="可选"
                  className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <Button type="submit" variant="primary" disabled={buyMut.isPending}>
                {buyMut.isPending ? "买入中..." : "确认买入"}
              </Button>
            </form>
            {buyMut.isError && (
              <p className="mt-2 text-xs text-down">失败 · {String(buyMut.error)}</p>
            )}
          </CardContent>
        </Card>
      )}

      {tradeTarget && (
        <Card>
          <CardContent className="py-4">
            <p className="mb-3 text-sm">
              {tradeSide === "buy" ? "加仓" : "减仓"} · {tradeTarget.name} ({tradeTarget.ts_code})
              {tradeTarget.position_size_shares != null && (
                <span className="ml-2 num text-xs text-text-secondary">
                  当前 {tradeTarget.position_size_shares} 股
                </span>
              )}
            </p>
            <form onSubmit={submitTrade} className="flex flex-wrap items-end gap-3">
              <div className="min-w-[120px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">成交价 *</label>
                <input type="number" step="0.01" value={tradePrice} onChange={(e) => setTradePrice(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[120px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">股数 *</label>
                <input type="number" step="100" value={tradeShares} onChange={(e) => setTradeShares(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[160px] flex-[2]">
                <label className="mb-1 block text-xs text-text-secondary">备注</label>
                <input type="text" value={tradeNote} onChange={(e) => setTradeNote(e.target.value)}
                  className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <Button type="submit" variant="primary"
                disabled={tradeSide === "buy" ? buyMut.isPending : sellMut.isPending}>
                确认{tradeSide === "buy" ? "加仓" : "卖出"}
              </Button>
              <Button type="button" variant="ghost" onClick={() => setTradeTarget(null)}>
                取消
              </Button>
            </form>
            {(tradeSide === "buy" ? buyMut.isError : sellMut.isError) && (
              <p className="mt-2 text-xs text-down">
                失败 · {String(tradeSide === "buy" ? buyMut.error : sellMut.error)}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* 加候选内联表单(ED3 失效矩阵接通) */}
      {showAdd && (
        <Card>
          <CardContent className="py-4">
            <form
              onSubmit={submitAdd}
              className="flex flex-wrap items-end gap-3"
            >
              <div className="min-w-[160px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">
                  ts_code
                </label>
                <input
                  type="text"
                  value={addCode}
                  onChange={(e) => setAddCode(e.target.value)}
                  placeholder="000001.SZ"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                />
              </div>
              <div className="min-w-[120px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">
                  触发价
                </label>
                <input
                  type="number"
                  step="0.01"
                  value={addPrice}
                  onChange={(e) => setAddPrice(e.target.value)}
                  placeholder="11.50"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                />
              </div>
              <Button
                type="submit"
                variant="primary"
                disabled={addCandidateMut.isPending}
              >
                {addCandidateMut.isPending ? "加中..." : "确认加"}
              </Button>
            </form>
            {addCandidateMut.isError && (
              <p className="mt-2 text-xs text-down">
                失败 · {String(addCandidateMut.error)}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {isError ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-flat">
            加载失败 · 刷新重试
          </CardContent>
        </Card>
      ) : isLoading ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-flat">
            加载中...
          </CardContent>
        </Card>
      ) : data ? (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            {/* 持仓 section */}
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <div className="flex items-center gap-2">
                  <Wallet className="h-4 w-4 text-text-secondary" />
                  <CardTitle>持仓</CardTitle>
                  <span className="num text-xs text-text-secondary">
                    {data.active_positions.length} 只
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => navigate("/analyze")}
                >
                  去分析 →
                </Button>
              </CardHeader>
              {data.active_positions.length === 0 ? (
                <CardContent>
                  <p className="py-6 text-center text-sm text-flat">暂无持仓</p>
                </CardContent>
              ) : (
                <CardContent className="pt-0">
                  <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                    {data.active_positions.map((p) => (
                      <CompactPositionCard
                        key={p.ts_code}
                        position={p}
                        onAdd={(pos) => openTradeForm(pos, "buy")}
                        onReduce={(pos) => openTradeForm(pos, "sell")}
                        onClose={openCloseForm}
                        onSyncAi={handleSyncAi}
                        syncing={syncingCode === p.ts_code}
                      />
                    ))}
                  </div>
                </CardContent>
              )}
            </Card>

            {/* 候选 section */}
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <div className="flex items-center gap-2">
                  <Bell className="h-4 w-4 text-text-secondary" />
                  <CardTitle>候选</CardTitle>
                  <span className="num text-xs text-text-secondary">
                    {data.candidates.length} 只
                  </span>
                </div>
                <CardDescription>触发价 + 方向, 接近时高亮</CardDescription>
              </CardHeader>
              {data.candidates.length === 0 ? (
                <CardContent>
                  <p className="py-6 text-center text-sm text-flat">暂无候选</p>
                </CardContent>
              ) : (
                <CardContent className="pt-0">
                  <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                    {data.candidates.map((c) => (
                      <CompactCandidateCard
                        key={c.ts_code}
                        candidate={c}
                        onArchive={handleArchive}
                        onPromote={openPromoteForm}
                      />
                    ))}
                  </div>
                </CardContent>
              )}
            </Card>
          </div>
        </>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <ListTree className="h-4 w-4 text-text-secondary" />
          <CardTitle>交易流水</CardTitle>
          <span className="num text-xs text-text-secondary">
            {tradesData?.length ?? 0} 条
          </span>
        </CardHeader>
        <CardContent>
          {!tradesData || tradesData.length === 0 ? (
            <p className="py-6 text-center text-sm text-flat">暂无交易记录</p>
          ) : (
            <div className="divide-y divide-border">
              {tradesData.map((t) => (
                <div key={t.trade_id} className="flex items-center justify-between gap-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${t.side === "buy" ? "bg-up/10 text-up" : "bg-down/10 text-down"}`}>
                        {t.side === "buy" ? "买入" : "卖出"}
                      </span>
                      <p className="truncate text-sm font-medium">{t.name || t.ts_code}</p>
                      <span className="num text-[11px] text-text-secondary">{t.ts_code}</span>
                    </div>
                    <p className="mt-0.5 num text-[11px] text-text-secondary">
                      {formatPrice(t.fill_price)} × {t.shares} 股 · {t.traded_at.slice(0, 16).replace("T", " ")}
                      {t.note && <span className="ml-1">· {t.note}</span>}
                    </p>
                  </div>
                  <div className="text-right">
                    {t.realized_pnl != null && (
                      <p className={`num text-sm ${t.realized_pnl >= 0 ? "text-up" : "text-down"}`}>
                        {t.realized_pnl >= 0 ? "+" : ""}{t.realized_pnl.toFixed(0)} 元
                      </p>
                    )}
                    <p className="num text-[11px] text-text-secondary">
                      {t.realized_pnl_pct != null
                        ? `${(t.realized_pnl_pct * 100).toFixed(2)}%`
                        : "—"}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 平仓复盘/归档入口(ED12 inventory, PR1b 占位) */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <History className="h-4 w-4 text-text-secondary" />
          <CardTitle>已平仓 / 归档</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-text-secondary">
            平仓后展示 AI 复盘 diagnosis(来自 <code className="num">/api/watchlist/close</code> 响应) + 触发 calibration 重算。
          </p>
          <p className="mt-2 text-xs text-flat">
            完整平仓流程 + diagnosis 展示 + 归档列表 — 后续 PR 接入。
          </p>
        </CardContent>
      </Card>

      {/* 平仓弹窗: 输成交价/日期/备注, 提交后展示 AI 复盘 diagnosis */}
      <Dialog
        open={closeTarget !== null || closeDiag !== null}
        onClose={() => {
          setCloseTarget(null);
          setCloseDiag(null);
        }}
        busy={closeMut.isPending}
        title={
          <span>
            平仓 <span className="num text-text-secondary">{closeTarget?.ts_code}</span>
          </span>
        }
      >
        {closeDiag !== null ? (
          // 提交成功 → 展示 AI 复盘, 关闭清场
          <div className="space-y-3">
            <p className="text-sm text-up">✓ 已平仓, AI 复盘如下:</p>
            <p className="whitespace-pre-wrap text-sm text-text-primary">{closeDiag}</p>
            <div className="flex justify-end">
              <Button variant="primary" size="sm" onClick={() => setCloseDiag(null)}>
                知道了
              </Button>
            </div>
          </div>
        ) : closeTarget ? (
          <form onSubmit={submitClose} className="space-y-3">
            <p className="text-xs text-flat">
              {closeTarget.name} · 当前持仓 {closeTarget.position_size_shares ?? "—"} 股
            </p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-xs text-text-secondary">成交价 *</label>
                <input
                  type="number"
                  step="0.01"
                  value={closePrice}
                  onChange={(e) => setClosePrice(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  autoFocus
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-text-secondary">成交日期 *</label>
                <input
                  type="date"
                  value={closeDate}
                  onChange={(e) => setCloseDate(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  required
                />
              </div>
            </div>
            <div>
              <label className="mb-1 block text-xs text-text-secondary">备注</label>
              <input
                type="text"
                value={closeNote}
                onChange={(e) => setCloseNote(e.target.value)}
                placeholder="可选"
                className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
              />
            </div>
            {closeMut.isError && (
              <p className="text-xs text-down">失败 · {String(closeMut.error)}</p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setCloseTarget(null)}
                disabled={closeMut.isPending}
              >
                取消
              </Button>
              <Button type="submit" variant="primary" size="sm" disabled={closeMut.isPending}>
                {closeMut.isPending ? "平仓中..." : "确认平仓"}
              </Button>
            </div>
          </form>
        ) : null}
      </Dialog>

      {/* promote 弹窗: 候选→持仓, 预填候选 trigger_price/stop_advice/target_advice */}
      <Dialog
        open={promoteTarget !== null}
        onClose={() => setPromoteTarget(null)}
        busy={promoteMut.isPending}
        title={
          <span>
            候选转持仓 <span className="num text-text-secondary">{promoteTarget?.ts_code}</span>
          </span>
        }
      >
        {promoteTarget && (
          <form onSubmit={submitPromote} className="space-y-3">
            <p className="text-xs text-flat">
              {promoteTarget.name} · 候选已成交, 用实际成交价转持仓。候选会被归档为
              archived_promoted。
            </p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-xs text-text-secondary">成交价 *</label>
                <input
                  type="number"
                  step="0.01"
                  value={promotePrice}
                  onChange={(e) => setPromotePrice(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  autoFocus
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-text-secondary">股数 *</label>
                <input
                  type="number"
                  step="100"
                  value={promoteShares}
                  onChange={(e) => setPromoteShares(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-text-secondary">止损 *</label>
                <input
                  type="number"
                  step="0.01"
                  value={promoteStop}
                  onChange={(e) => setPromoteStop(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-text-secondary">目标 *</label>
                <input
                  type="number"
                  step="0.01"
                  value={promoteTargetPrice}
                  onChange={(e) => setPromoteTargetPrice(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
                  required
                />
              </div>
            </div>
            {promoteMut.isError && (
              <p className="text-xs text-down">失败 · {String(promoteMut.error)}</p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setPromoteTarget(null)}
                disabled={promoteMut.isPending}
              >
                取消
              </Button>
              <Button type="submit" variant="primary" size="sm" disabled={promoteMut.isPending}>
                {promoteMut.isPending ? "转入中..." : "确认转持仓"}
              </Button>
            </div>
          </form>
        )}
      </Dialog>
    </div>
  );
}
