import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, Info, Loader2 } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Dialog,
} from "@/components/base";
import { CandlestickChart } from "@/components/a-share/CandlestickChart";
import {
  CHAN_BSP_LABEL,
  getChanStructure,
  type ChanDecision,
  type ChanDecisionIneligibleReason,
  type ChanFreq,
  type ChanStructure,
} from "@/api/chan";
import { qk } from "@/api/query-keys";
import { ApiError } from "@/api/client";
import { useAddCandidate } from "@/api/mutations";
import { getWatchlist } from "@/api/watchlist";
import { cn, formatPrice } from "@/lib/utils";

/**
 * /chan 缠论技术分析页
 *
 * 周期切换（30/60/日/周）+ K 线 + 笔/中枢/买卖点叠加 + 摘要面板。
 * 口径披露（双重免责）：
 *   1. 买卖点为"本级别近似，未经次级别确认"（纯单级别 BSP 理论近似）
 *   2. 最后一笔未完成会随行情重画（repaint 是缠论固有属性）
 * 盘中口径：缠论页按昨日收盘计算（vs 行情条实时价），摘要显式标注。
 */

const FREQS: { key: ChanFreq; label: string }[] = [
  { key: "30", label: "30分" },
  { key: "60", label: "60分" },
  { key: "D", label: "日线" },
  { key: "W", label: "周线" },
];

const ZS_BREAK_LABEL: Record<string, { text: string; cls: string }> = {
  up: { text: "突破向上", cls: "text-up" },
  down: { text: "跌破向下", cls: "text-down" },
  inside: { text: "中枢内", cls: "text-text-secondary" },
  none: { text: "无已确认中枢", cls: "text-text-secondary" },
};

const DECISION_STATE_META: Record<
  ChanStructure["decision"]["state"],
  { text: string; cls: string }
> = {
  watching: { text: "观察中", cls: "text-text-secondary" },
  pending: { text: "等待确认", cls: "text-flat" },
  confirmed: { text: "已确认", cls: "text-up" },
  invalid: { text: "已失效", cls: "text-down" },
};

const DECISION_BIAS_META: Record<
  ChanStructure["decision"]["bias"],
  { text: string; cls: string }
> = {
  long: { text: "偏多", cls: "text-up" },
  neutral: { text: "中性", cls: "text-text-secondary" },
  risk: { text: "风险", cls: "text-down" },
};

const DECISION_INELIGIBLE_REASON_LABEL: Record<
  ChanDecisionIneligibleReason,
  string
> = {
  no_actionable_structure: "暂无可执行的做多结构",
  signal_bar_missing: "未找到信号对应的已完成 K 线",
  signal_invalidated: "当前收盘已跌破结构失效价",
  stale_signal: "信号已超过 10 根已完成 K 线",
  risk_structure: "当前结构为卖点或向下跌破风险",
};

const CANDIDATE_INPUT_CLASS =
  "w-full rounded-md border border-border bg-bg-card px-3 py-1.5 font-mono text-sm outline-none focus:border-text-secondary";

function decisionInputPrice(value: number | null) {
  return value == null ? "" : value.toFixed(3).replace(/0$/, "");
}

function decisionIneligibleReason(reason: ChanDecision["ineligible_reason"]) {
  return reason == null ? null : DECISION_INELIGIBLE_REASON_LABEL[reason];
}

function candidateSetupLabel(decision: ChanDecision) {
  return decision.setup === "zs_breakout"
    ? "中枢突破回踩"
    : `缠论${CHAN_BSP_LABEL[decision.bsp_type ?? ""] ?? "买点"}`;
}

function candidateDecisionKey(data: ChanStructure) {
  const decision = data.decision;
  return JSON.stringify([
    data.ts_code,
    data.freq,
    decision.state,
    decision.setup,
    decision.bsp_type,
    decision.signal_dt,
    decision.confirm_price,
    decision.invalidation_price,
    decision.trigger_price,
    decision.trigger_low,
    decision.trigger_high,
  ]);
}

export function ChanPage() {
  // ?ts_code= 预填 + 自动加载（v1.1 持仓/候选卡跳转的留口；v1 先通 URL）
  const [searchParams] = useSearchParams();
  const initial = searchParams.get("ts_code") || "603019.SH";
  const [tsCode, setTsCode] = useState(initial);
  const [committed, setCommitted] = useState(initial);
  const [freq, setFreq] = useState<ChanFreq>("D");

  const isBj = committed.endsWith(".BJ");
  const effectiveFreq = isBj && (freq === "30" || freq === "60") ? "D" : freq;

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk.chan(committed, effectiveFreq),
    queryFn: () => getChanStructure(committed, effectiveFreq),
    enabled: !!committed,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const apiError = error instanceof ApiError ? error : null;
  const isCzscMissing = apiError?.status === 501;
  const isDataFail = apiError?.status === 502;
  const insufficient = data?.summary.reason === "insufficient_bars";

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <div className="mb-4 flex items-center gap-2">
        <Activity className="h-5 w-5 text-text-secondary" />
        <h1 className="font-serif text-xl font-semibold">缠论</h1>
        <span className="text-sm text-text-secondary">
          笔 / 中枢 / 买卖点（czsc 分段）
        </span>
      </div>

      {/* 输入 + 周期切换 */}
      <Card className="mb-4">
        <CardContent className="flex flex-wrap items-center gap-3 py-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const v = tsCode.trim().toUpperCase();
              if (v) setCommitted(v);
            }}
            className="flex items-center gap-2"
          >
            <input
              value={tsCode}
              onChange={(e) => setTsCode(e.target.value)}
              placeholder="代码 如 603019.SH"
              className="w-44 rounded-md border border-border bg-bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-text-secondary"
            />
            <button
              type="submit"
              className="rounded-md bg-text-primary px-3 py-1.5 text-sm text-bg-card"
            >
              查看
            </button>
          </form>
          <div className="flex items-center gap-1 rounded-md border border-border p-0.5">
            {FREQS.map((f) => {
              const disabled = isBj && (f.key === "30" || f.key === "60");
              return (
                <button
                  key={f.key}
                  disabled={disabled}
                  onClick={() => setFreq(f.key)}
                  title={disabled ? "BJ 分钟数据暂不可用" : undefined}
                  className={cn(
                    "rounded px-2.5 py-1 text-sm transition-colors",
                    freq === f.key
                      ? "bg-text-primary text-bg-card"
                      : "text-text-secondary hover:bg-bg-base",
                    disabled && "cursor-not-allowed opacity-40",
                  )}
                >
                  {f.label}
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* 错误态 */}
      {isCzscMissing && (
        <Notice tone="warn" title="缠论引擎不可用（501）">
          czsc 未安装或导入失败。后端需 <code>pip install czsc==0.10.12</code>。
        </Notice>
      )}
      {isDataFail && (
        <Notice tone="warn" title="数据获取失败（502）">
          {apiError?.message ?? "数据源暂不可用，稍后重试。"}
          {isBj && (freq === "30" || freq === "60") && "（BJ 分钟数据暂不支持）"}
        </Notice>
      )}

      {/* 降级态：bars 不足 */}
      {insufficient && (
        <Notice tone="info" title="历史数据不足">
          该票可用 K 线不足 30 根，无法计算缠论结构。K 线照常展示。
        </Notice>
      )}

      {/* 除权警示 */}
      {data?.summary.ex_div_gap && (
        <Notice tone="warn" title="近期有除权除息跳空">
          不复权数据上存在机械跳空，<strong>zs_break 已只用跳空后形成的中枢</strong>
          （除权前中枢价位与现价不可比）。跨除权笔的幅度可能失真。
        </Notice>
      )}

      {/* 图表 */}
      {data && !isCzscMissing && (
        <Card className="mb-4">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 font-mono">
              {data.ts_code}
              <span className="text-sm font-normal text-text-secondary">
                {FREQS.find((f) => f.key === data.freq)?.label}
              </span>
            </CardTitle>
            <CardDescription>
              数据截至 {data.summary.bars_end_dt ?? "—"}
              （只使用已完成 K 线
              {data.freq === "D" ? "，盘中为昨日收盘" : ""}）
            </CardDescription>
          </CardHeader>
          <CardContent>
            {!insufficient && (
              <CandlestickChart structure={data} className="w-full" />
            )}
            {insufficient && data.bars.length > 0 && (
              <CandlestickChart structure={data} className="w-full" height={300} />
            )}
          </CardContent>
        </Card>
      )}

      {/* 决策卡（保留原结构摘要指标） */}
      {data && !insufficient && (
        <DecisionPanel
          data={data}
          refetchDecision={async () => {
            const result = await refetch({ throwOnError: true });
            if (result.data == null) throw new Error("刷新结果为空");
            return result.data;
          }}
        />
      )}

      {/* loading */}
      {isLoading && (
        <div className="flex items-center justify-center py-12 text-text-secondary">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          计算缠论结构中…
        </div>
      )}

      {/* 双重免责声明 */}
      <div className="mt-6 rounded-md border border-border bg-bg-card p-3 text-xs text-text-secondary">
        <p className="mb-1 flex items-center gap-1">
          <Info className="h-3.5 w-3.5" />
          口径披露
        </p>
        <ul className="ml-5 list-disc space-y-0.5">
          <li>
            买卖点为<strong>本级别近似，未经次级别确认</strong>
            （纯单级别 BSP 是理论近似，教科书三类买卖点需次级别回抽确认）。
          </li>
          <li>
            最后一根笔<strong>未完成时会随行情重画</strong>
            （repaint 是缠论固有属性，图中虚线笔为未确认）。
          </li>
          <li>多级别联立由人肉眼做（日线 3 买候选 → 切 30 分验回抽）。</li>
          <li>
            结构基于最近 250 根窗口；窗口滑动后，左端历史的笔与中枢可能重新分段。
          </li>
        </ul>
      </div>
    </div>
  );
}

function DecisionPanel({
  data,
  refetchDecision,
}: {
  data: ChanStructure;
  refetchDecision: () => Promise<ChanStructure>;
}) {
  const s = data.summary;
  const decision = data.decision;
  const mutation = useAddCandidate();
  const {
    data: watchlist,
    isError: isWatchlistError,
  } = useQuery({
    queryKey: qk.watchlist,
    queryFn: getWatchlist,
    retry: false,
  });
  const [candidateOpen, setCandidateOpen] = useState(false);
  const [triggerPrice, setTriggerPrice] = useState("");
  const [triggerLow, setTriggerLow] = useState("");
  const [triggerHigh, setTriggerHigh] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [note, setNote] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [openedDecisionKey, setOpenedDecisionKey] = useState<string | null>(null);
  const [isRefreshingCandidate, setIsRefreshingCandidate] = useState(false);
  const zb = ZS_BREAK_LABEL[s.zs_break ?? "none"];
  const stateMeta = DECISION_STATE_META[decision.state];
  const biasMeta = DECISION_BIAS_META[decision.bias];
  const lastBiDir = s.last_bi_direction === "up" ? "向上" : s.last_bi_direction === "down" ? "向下" : "—";
  const freqLabel = FREQS.find((item) => item.key === data.freq)?.label ?? data.freq;
  const setupLabel = candidateSetupLabel(decision);
  const ineligibleReason = decisionIneligibleReason(decision.ineligible_reason);
  const existingCandidate = watchlist?.candidates.some(
    (candidate) => candidate.ts_code === data.ts_code,
  );
  const decisionKey = candidateDecisionKey(data);

  const clearCandidateForm = () => {
    setTriggerPrice("");
    setTriggerLow("");
    setTriggerHigh("");
    setStopPrice("");
    setNote("");
    setFormError(null);
  };

  const openCandidateDialog = () => {
    setTriggerPrice(decisionInputPrice(decision.trigger_price));
    setTriggerLow(decisionInputPrice(decision.trigger_low));
    setTriggerHigh(decisionInputPrice(decision.trigger_high));
    setStopPrice(decisionInputPrice(decision.invalidation_price));
    setNote(
      `${freqLabel} · 信号 ${decision.signal_dt ?? "—"} · 确认 ${decisionInputPrice(decision.confirm_price) || "—"} / 失效 ${decisionInputPrice(decision.invalidation_price) || "—"}`,
    );
    setFormError(null);
    setSuccessMessage(null);
    setOpenedDecisionKey(decisionKey);
    setCandidateOpen(true);
  };

  useEffect(() => {
    if (!candidateOpen || openedDecisionKey == null || openedDecisionKey === decisionKey) return;
    setCandidateOpen(false);
    setOpenedDecisionKey(null);
    setFormError(null);
  }, [candidateOpen, decisionKey, openedDecisionKey]);

  const submitCandidate = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError(null);

    if (openedDecisionKey == null || openedDecisionKey !== decisionKey) {
      setCandidateOpen(false);
      setOpenedDecisionKey(null);
      return;
    }
    const trigger = Number(triggerPrice);
    if (!triggerPrice.trim() || !Number.isFinite(trigger) || trigger <= 0) {
      setFormError("触发价必须大于 0");
      return;
    }

    const optionalPrice = (value: string, label: string) => {
      if (!value.trim()) return undefined;
      const parsed = Number(value);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`${label}必须大于 0`);
      }
      return parsed;
    };

    let low: number | undefined;
    let high: number | undefined;
    let stop: number | undefined;
    try {
      low = optionalPrice(triggerLow, "区间下沿");
      high = optionalPrice(triggerHigh, "区间上沿");
      stop = optionalPrice(stopPrice, "止损价");
    } catch (validationError) {
      setFormError(validationError instanceof Error ? validationError.message : "价格格式无效");
      return;
    }

    if (low != null && high != null && low > high) {
      setFormError("区间下沿不能高于区间上沿");
      return;
    }

    let freshData: ChanStructure;
    setIsRefreshingCandidate(true);
    try {
      freshData = await refetchDecision();
    } catch (refreshError) {
      setFormError(
        `刷新当前缠论决策失败：${refreshError instanceof Error ? refreshError.message : String(refreshError)}`,
      );
      return;
    } finally {
      setIsRefreshingCandidate(false);
    }

    const freshDecisionKey = candidateDecisionKey(freshData);
    if (openedDecisionKey == null || openedDecisionKey !== freshDecisionKey) {
      setCandidateOpen(false);
      setOpenedDecisionKey(null);
      return;
    }
    const freshDecision = freshData.decision;
    if (!freshDecision.candidate_eligible) {
      setFormError(
        decisionIneligibleReason(freshDecision.ineligible_reason)
          ?? "当前结构已不可加入候选",
      );
      return;
    }

    mutation.mutate(
      {
        ts_code: freshData.ts_code,
        name: "",
        trigger_price: trigger,
        trigger_direction: freshDecision.setup === "zs_breakout" ? "below" : "above",
        trigger_low: low,
        trigger_high: high,
        stop_advice: stop,
        note,
        strategy: "chan",
        setup: candidateSetupLabel(freshDecision),
      },
      {
        onSuccess: () => {
          clearCandidateForm();
          setCandidateOpen(false);
          setOpenedDecisionKey(null);
          setSuccessMessage("已加入候选");
        },
        onError: (mutationError) => {
          setFormError(
            `加入候选失败：${mutationError instanceof Error ? mutationError.message : String(mutationError)}`,
          );
        },
      },
    );
  };

  return (
    <>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">缠论决策卡</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("rounded-full bg-bg-base px-2.5 py-1 font-medium", biasMeta.cls)}>
            {biasMeta.text}
          </span>
          <span className={cn("rounded-full bg-bg-base px-2.5 py-1 font-medium", stateMeta.cls)}>
            {stateMeta.text}
          </span>
          {decision.bars_since_signal != null && (
            <span className="text-xs text-text-secondary">
              距今 {decision.bars_since_signal} 根
            </span>
          )}
        </div>

        <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
          <Metric
            label="确认价"
            value={decision.confirm_price == null ? "—" : formatPrice(decision.confirm_price)}
          />
          <Metric
            label="失效价"
            value={decision.invalidation_price == null ? "—" : formatPrice(decision.invalidation_price)}
          />
          <Metric label="信号时间" value={decision.signal_dt ?? "—"} />
          <Metric
            label="触发价"
            value={decision.trigger_price == null ? "—" : formatPrice(decision.trigger_price)}
          />
        </div>

        {decision.basis.length > 0 && (
          <ul className="ml-5 list-disc space-y-1 text-text-secondary">
            {decision.basis.map((item, index) => (
              <li key={`${index}-${item}`}>{item}</li>
            ))}
          </ul>
        )}

        {decision.state === "confirmed" && (
          <p className="rounded-md bg-bg-base px-3 py-2 text-xs text-text-secondary">
            已确认不等于可追高，请按候选触发条件等待机会。
          </p>
        )}
        {!decision.candidate_eligible && ineligibleReason && (
          <p className="text-xs text-down">{ineligibleReason}</p>
        )}

        <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-t border-border pt-3 sm:grid-cols-4">
          <Metric label="最新笔方向" value={lastBiDir} />
          <Metric label="最新笔天数" value={s.last_bi_days != null ? `${s.last_bi_days} 根` : "—"} />
          <Metric
            label="最新笔状态"
            value={s.last_bi_confirmed == null ? "—" : s.last_bi_confirmed ? "已确认" : "未确认（延伸中）"}
          />
          <Metric label="中枢突破" value={zb.text} valueClass={zb.cls} />
          {s.last_confirmed_zs && (
            <Metric
              label="最后已确认中枢"
              value={`${formatPrice(s.last_confirmed_zs.zd)} ~ ${formatPrice(s.last_confirmed_zs.zg)}`}
            />
          )}
          {s.extending_zs && (
            <Metric
              label="延伸中中枢"
              value={`${formatPrice(s.extending_zs.zd)} ~ ${formatPrice(s.extending_zs.zg)}`}
              valueClass="text-text-secondary"
            />
          )}
          {s.recent_bsp && (
            <Metric
              label="最近买卖点"
              value={`${CHAN_BSP_LABEL[s.recent_bsp.type] ?? s.recent_bsp.type} @ ${formatPrice(s.recent_bsp.price)}`}
              valueClass={s.recent_bsp.type.endsWith("buy") ? "text-up" : "text-down"}
            />
          )}
          <Metric label="笔 / 中枢 / 买卖点" value={`${data.bi_list.length} / ${data.zs_list.length} / ${data.bsp_list.length}`} />
        </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <button
            type="button"
            disabled={!decision.candidate_eligible}
            onClick={openCandidateDialog}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-base disabled:cursor-not-allowed disabled:opacity-50"
          >
            加入候选
          </button>
          <Link
            to="/analyze"
            onClick={() => {
              sessionStorage.setItem(
                "apex.analyze.state",
                JSON.stringify({
                  tsCode: data.ts_code,
                  committedCode: null,
                  latestVerdict: null,
                }),
              );
            }}
            className="rounded-md bg-text-primary px-3 py-1.5 text-sm text-bg-card"
          >
            发起 AI 分析
          </Link>
          {successMessage && <span className="text-xs text-up">{successMessage}</span>}
        </div>
        </CardContent>
      </Card>

      {candidateOpen && (
        <Dialog
          open
          onClose={() => {
            setCandidateOpen(false);
            setOpenedDecisionKey(null);
          }}
          busy={mutation.isPending || isRefreshingCandidate}
          title="确认加入候选"
        >
        <form onSubmit={submitCandidate} className="space-y-4">
          <div className="rounded-md bg-bg-base p-3 text-xs text-text-secondary">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <span className="font-mono text-text-primary">{data.ts_code}</span>
              <span>{freqLabel}</span>
              <span>{setupLabel}</span>
              <span>信号 {decision.signal_dt ?? "—"}</span>
            </div>
          </div>

          {existingCandidate && (
            <p className="rounded-md border border-flat/30 bg-flat/5 px-3 py-2 text-xs text-text-primary">
              将覆盖原候选参数
            </p>
          )}
          {isWatchlistError && (
            <p className="rounded-md border border-flat/30 bg-flat/5 px-3 py-2 text-xs text-text-secondary">
              候选列表读取失败，无法确认是否已有候选；仍可提交。
            </p>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="chan-trigger-price" className="mb-1 block text-xs text-text-secondary">
                触发价
              </label>
              <input
                id="chan-trigger-price"
                type="number"
                step="0.001"
                value={triggerPrice}
                onChange={(event) => setTriggerPrice(event.target.value)}
                className={CANDIDATE_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="chan-stop-price" className="mb-1 block text-xs text-text-secondary">
                止损价
              </label>
              <input
                id="chan-stop-price"
                type="number"
                step="0.001"
                value={stopPrice}
                onChange={(event) => setStopPrice(event.target.value)}
                className={CANDIDATE_INPUT_CLASS}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="chan-trigger-low" className="mb-1 block text-xs text-text-secondary">
                区间下沿
              </label>
              <input
                id="chan-trigger-low"
                type="number"
                step="0.001"
                value={triggerLow}
                onChange={(event) => setTriggerLow(event.target.value)}
                className={CANDIDATE_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="chan-trigger-high" className="mb-1 block text-xs text-text-secondary">
                区间上沿
              </label>
              <input
                id="chan-trigger-high"
                type="number"
                step="0.001"
                value={triggerHigh}
                onChange={(event) => setTriggerHigh(event.target.value)}
                className={CANDIDATE_INPUT_CLASS}
              />
            </div>
          </div>

          <div>
            <label htmlFor="chan-note" className="mb-1 block text-xs text-text-secondary">
              备注
            </label>
            <input
              id="chan-note"
              type="text"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              className={CANDIDATE_INPUT_CLASS}
            />
          </div>

          <p className="text-xs text-text-secondary">
            目标价保持为空，可在后续 AI 分析或人工复核后补充。
          </p>
          {formError && <p className="text-xs text-down">{formError}</p>}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={mutation.isPending || isRefreshingCandidate}
              onClick={() => {
                setCandidateOpen(false);
                setOpenedDecisionKey(null);
              }}
              className="rounded-md px-3 py-1.5 text-sm text-text-secondary hover:bg-bg-base disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={mutation.isPending || isRefreshingCandidate}
              className="rounded-md bg-text-primary px-3 py-1.5 text-sm text-bg-card disabled:opacity-50"
            >
              {isRefreshingCandidate ? "刷新决策中…" : mutation.isPending ? "加入中…" : "确认加入候选"}
            </button>
          </div>
          </form>
        </Dialog>
      )}
    </>
  );
}

function Metric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-xs text-text-secondary">{label}</div>
      <div className={cn("font-mono", valueClass)}>{value}</div>
    </div>
  );
}

function Notice({
  tone,
  title,
  children,
}: {
  tone: "warn" | "info";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "mb-4 flex items-start gap-2 rounded-md border p-3 text-sm",
        tone === "warn"
          ? "border-up/30 bg-up/5 text-text-primary"
          : "border-border bg-bg-card text-text-primary",
      )}
    >
      <AlertTriangle className={cn("mt-0.5 h-4 w-4 shrink-0", tone === "warn" ? "text-up" : "text-text-secondary")} />
      <div>
        <div className="font-medium">{title}</div>
        <div className="text-text-secondary">{children}</div>
      </div>
    </div>
  );
}
