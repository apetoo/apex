import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw, Wallet, Bell, History } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { PositionCard, CandidateCard } from "@/components/a-share";
import { getWatchlist } from "@/api/watchlist";
import { useAddCandidate } from "@/api/mutations";
import { qk } from "@/api/query-keys";

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

  // 加候选 简单 prompt
  const [addCode, setAddCode] = useState("");
  const [addPrice, setAddPrice] = useState("");
  const [showAdd, setShowAdd] = useState(false);

  const submitAdd = (e?: React.FormEvent) => {
    e?.preventDefault();
    const tsCode = addCode.trim().toUpperCase();
    const price = Number(addPrice);
    if (!tsCode || !price) return;
    addCandidateMut.mutate(
      {
        ts_code: tsCode,
        name: tsCode,
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
            onClick={() => setShowAdd((s) => !s)}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            加候选
          </Button>
        </div>
      </div>

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
                {data.active_positions.map((p) => (
                  <PositionCard key={p.ts_code} position={p} />
                ))}
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
                {data.candidates.map((c) => (
                  <CandidateCard key={c.ts_code} candidate={c} />
                ))}
              </CardContent>
            )}
          </Card>
        </>
      ) : null}

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
    </div>
  );
}
