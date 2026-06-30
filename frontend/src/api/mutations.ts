import { useMutation, useQueryClient, type UseMutationResult } from "@tanstack/react-query";
import {
  addCandidate,
  addPosition,
  replacePosition,
  promoteCandidate,
  closePosition,
  archiveEntry,
  buy,
  sell,
  updateAdvice,
  type AddCandidatePayload,
  type AddPositionPayload,
  type PromotePayload,
  type ClosePositionPayload,
  type ArchivePayload,
  type BuyPayload,
  type SellPayload,
  type BuyResponse,
  type SellResponse,
  type UpdateAdvicePayload,
} from "./watchlist";
import { qk } from "./query-keys";

/**
 * React Query mutations + ED3 失效矩阵接通
 *
 * 每个 mutation 在 onSuccess 按 ED3 矩阵 invalidate 对应 query keys,
 * 避免用户看到缓存的旧数据(平仓后列表不更新等)。
 *
 * ED3 SSOT 见 query-keys.test.ts。
 *
 * 404 409 等错误通过 onError 抛, 由调用方 toast/弹对话框(PR1b 占位按钮后续接)。
 */

/* ── 失效键表(ED3 矩阵) ────────────────────────────── */
const INVALIDATE: Record<string, string[][]> = {
  // 加持仓 → watchlist + account(总资产变) + triggers(候选/触发器)
  addPosition: [[...qk.watchlist], [...qk.account], [...qk.triggers]],
  replacePosition: [[...qk.watchlist], [...qk.account], [...qk.triggers]],
  // 加候选 → watchlist + triggers
  addCandidate: [[...qk.watchlist], [...qk.triggers]],
  // promote 候选→持仓 → 影响最大
  promoteCandidate: [
    [...qk.watchlist],
    [...qk.account],
    [...qk.triggers],
    [...qk.closed],
  ],
  // 平仓 → 影响最大(触发 postmortem + calibration); close_position 现在也写 sell trade, 故失效 trades
  closePosition: [
    [...qk.watchlist],
    [...qk.trades],
    [...qk.account],
    [...qk.closed],
    [...qk.calibration],
    [...qk.triggers],
  ],
  // 买入(开仓/加仓) → 持仓 + 流水(账户总风险也变)
  buy: [[...qk.watchlist], [...qk.trades], [...qk.account], [...qk.triggers]],
  // 卖出:减仓 → 持仓+流水;卖光 → 还影响 closed+calibration。统一全失效, 简单正确。
  sell: [
    [...qk.watchlist],
    [...qk.trades],
    [...qk.account],
    [...qk.closed],
    [...qk.calibration],
    [...qk.triggers],
  ],
  // 归档 → watchlist + triggers
  archiveEntry: [[...qk.watchlist], [...qk.triggers]],
  // 更新止损/目标(advice) → 只影响 watchlist(持仓卡显示)
  updateAdvice: [[...qk.watchlist]],
};

function invalidateAll(
  qc: ReturnType<typeof useQueryClient>,
  op: keyof typeof INVALIDATE,
) {
  const keys = INVALIDATE[op];
  for (const k of keys) {
    qc.invalidateQueries({ queryKey: k });
  }
}

/* ── Hooks ────────────────────────────────────────────── */

export function useAddCandidate(): UseMutationResult<
  { message: string; ts_code: string },
  Error,
  AddCandidatePayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: addCandidate,
    onSuccess: () => invalidateAll(qc, "addCandidate"),
  });
}

export function useAddPosition(): UseMutationResult<
  { message: string; ts_code: string },
  Error,
  AddPositionPayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: addPosition,
    // 409 是真业务错误(duplicate), 不重试
    retry: false,
    onSuccess: () => invalidateAll(qc, "addPosition"),
  });
}

export function useReplacePosition(): UseMutationResult<
  { message: string; ts_code: string },
  Error,
  AddPositionPayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: replacePosition,
    onSuccess: () => invalidateAll(qc, "replacePosition"),
  });
}

export function usePromoteCandidate(): UseMutationResult<
  { message: string; ts_code: string },
  Error,
  PromotePayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: promoteCandidate,
    onSuccess: () => invalidateAll(qc, "promoteCandidate"),
  });
}

export function useClosePosition(): UseMutationResult<
  { message: string; record: unknown; diagnosis: string },
  Error,
  ClosePositionPayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: closePosition,
    onSuccess: () => invalidateAll(qc, "closePosition"),
  });
}

export function useBuy(): UseMutationResult<BuyResponse, Error, BuyPayload> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: buy,
    retry: false,
    onSuccess: () => invalidateAll(qc, "buy"),
  });
}

export function useSell(): UseMutationResult<SellResponse, Error, SellPayload> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: sell,
    retry: false,
    onSuccess: () => invalidateAll(qc, "sell"),
  });
}

export function useArchiveEntry(): UseMutationResult<
  { message: string; moved: boolean },
  Error,
  ArchivePayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: archiveEntry,
    onSuccess: () => invalidateAll(qc, "archiveEntry"),
  });
}

/**
 * 同步 AI advice 到持仓: 调用方先 getLatestJournal 拿到 stop_loss/target,
 * 再用本 mutation 覆盖写入。变量是 { ts_code, ...payload }, 包一层适配 react-query 单参 mutationFn。
 */
export function useUpdateAdvice(): UseMutationResult<
  { message: string; position: unknown },
  Error,
  UpdateAdvicePayload
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars) => updateAdvice(vars.ts_code, vars),
    onSuccess: () => invalidateAll(qc, "updateAdvice"),
  });
}
