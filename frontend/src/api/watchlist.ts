import { api } from "./client";
import type { RuleChecklist } from "@/lib/trading-system";

/**
 * Watchlist API
 *
 * 对应 backend/routers/watchlist.py:
 *   GET    /api/watchlist               -> { active_positions, candidates, archived }
 *   POST   /api/watchlist/positions     -> 加持仓
 *   POST   /api/watchlist/positions/replace -> 替换重复持仓(409 后用)
 *   POST   /api/watchlist/candidates    -> 加候选
 *   POST   /api/watchlist/promote       -> 候选->持仓(实际成交)
 *   POST   /api/watchlist/close         -> 平仓(返回 diagnosis)
 *   POST   /api/watchlist/archive       -> 归档
 *   GET    /api/watchlist/closed        -> 已平仓
 *   POST   /api/watchlist/migrate       -> 一次性迁移
 *
 * 字段定义见 apex/watchlist.py(单 user, 文件存储)。
 * ED3 失效矩阵的失效目标 = ['watchlist', 'account', 'triggers', 'closed', 'calibration']
 */

/** v1.1.0: ladder 单档（加/减仓触发计划，AI 重新分析时演进） */
export interface ScalePlanItem {
  level?: number;
  trigger_price?: number;
  action?: "add" | "trim";
  shares?: number;
  pct?: number;
  new_stop?: number;
  reason?: string;
  /** B3 sim 触发后标记，防同档每 bar 重触发 */
  executed?: boolean;
}

/** v1.1.0: 持仓 ladder 快照（存于 active_positions.plan，AI 建议演进/B3 sim 消费） */
export interface PositionPlan {
  scale_plan: ScalePlanItem[];
  /** B1 单一默认 'single_v1'；B2 按 playstyle 分桶 */
  doctrine: string;
  updated_at?: string | null;
  /** B1 增强：最近 position_action 快照（持仓卡"现在 vs 未来"，区分当前决策与条件触发计划） */
  last_action?: "hold" | "add" | "trim" | "exit" | null;
  last_new_stop?: number | null;
  last_stop_before?: number | null;
}

export interface ActivePosition {
  ts_code: string;
  name: string;
  entry_price: number;
  avg_cost?: number;
  entry_date: string;
  stop_loss?: number;
  target?: number;
  trigger_price: number | null;
  trigger_direction: "below" | "above";
  expires_at: string;
  status: string;
  position_size_shares?: number;
  risk_amount?: number;
  calibrated_confidence?: number;
  strategy?: string;
  trigger_low?: number | null;
  trigger_high?: number | null;
  note?: string;
  /** ADR-0001: 交易原型（candidate 继承或 promote 时确认） */
  setup?: string;
  /** ADR-0001: 自录 Rule 检查表，close 后守规算分 */
  rule_checklist?: RuleChecklist;
  /** v1.1.0: 持仓 ladder 快照（开仓空，AI 重新分析时演进，平仓 null） */
  plan?: PositionPlan | null;
}

export interface ExpiresMeta {
  /** 实际生效的过期天数 */
  expires_days: number;
  /** 过期天数怎么来的: vol_based=波动率算 / manual=手动覆盖 / fallback=数据缺失退回默认 */
  method: "vol_based" | "manual" | "fallback";
  /** 近 20 日日收益率标准差(小数), vol_based 才有 */
  realized_vol?: number;
  /** 加候选时现价到 trigger 的距离百分比, vol_based 才有 */
  distance_pct?: number;
  /** 临界交易日数 t*=(d/σ)², vol_based 才有 */
  t_trading?: number;
}

export interface Candidate {
  ts_code: string;
  name: string;
  trigger_price: number;
  trigger_direction: "below" | "above";
  expires_at: string;
  /** 过期天数归因(动态过期特性, 历史候选可能没有) */
  expires_meta?: ExpiresMeta;
  /** 手动续期次数, 防僵尸候选提醒用 */
  renew_count?: number;
  note: string;
  stop_advice: number;
  target_advice: number;
  strategy?: string;
  trigger_low?: number | null;
  trigger_high?: number | null;
  /** ADR-0001: 交易原型，可由 AI verdict setup_tag 预填 */
  setup?: string;
}

export interface WatchlistData {
  active_positions: ActivePosition[];
  candidates: Candidate[];
  archived: Array<Record<string, unknown>>;
}

export interface Trade {
  trade_id: string;
  ts_code: string;
  name: string;
  side: "buy" | "sell";
  fill_price: number;
  shares: number;
  amount: number;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  avg_cost_after: number | null;
  shares_after: number | null;
  strategy: string | null;
  regime: string | null;
  journal_ref: { verdict: string | null; confidence: number | null; analyzed_at: string | null } | null;
  note: string;
  traded_at: string;
}

export interface BuyPayload {
  ts_code: string;
  fill_price: number;
  shares: number;
  stop_loss?: number;
  target?: number;
  note?: string;
  strategy?: string;
}

export interface SellPayload {
  ts_code: string;
  fill_price: number;
  shares: number;
  exit_reason?: string;
  note?: string;
  postmortem?: boolean;
}

export interface BuyResponse {
  position: ActivePosition;
  trade: Trade;
}

export interface SellResponsePartial {
  position: ActivePosition;
  trade: Trade;
}

export interface SellResponseClosed {
  trade: Trade;
  closed_record: Record<string, unknown>;
  diagnosis: PostmortemDiagnosis | null;
}

export type SellResponse = SellResponsePartial | SellResponseClosed;

/**
 * 平仓复盘 diagnosis（apex/postmortem.py:run_and_patch 返回的 dict）。
 * 机械字段由代码算好（outcome_class / nearly_hit_target / stop_hit_too_tight /
 * calibration_bucket / target_distance_max_pct），AI 只填因果解释。
 */
export interface PostmortemDiagnosis {
  /** "win" | "loss"（机械判定：realized_pnl >= 0） */
  outcome_class: "win" | "loss" | string;
  /** 持仓期间最高价到 entry 的距离（小数），衡量「涨了多少」 */
  target_distance_max_pct?: number | null;
  /** 亏损但高点曾 >= 目标*95%（差一点到目标） */
  nearly_hit_target?: boolean;
  /** 低点跌破止损但高点又回升过进场价（止损挂太紧） */
  stop_hit_too_tight?: boolean;
  /** verdict@bucket 校准桶标签，如 "bullish@mid" */
  calibration_bucket?: string;
  /** AI 点出它判断对的因素 */
  ai_correctly_identified?: string[];
  /** AI 点出它漏掉的因素 */
  ai_missed?: string[];
  /** 一句话教训 */
  lesson?: string;
  /** AI 复盘正文（含 markdown，走 <Markdown> 渲染） */
  ai_diagnosis_text?: string;
  /** 出场后回看 K 线天数 */
  post_exit_kline_used?: number;
  /** 用的 LLM model id */
  model?: string;
  /** 诊断时间（ISO，CN 时区） */
  diagnosed_at?: string;
}

/**
 * 已平仓记录（GET /api/watchlist/closed）。
 * 形状对齐 apex/watchlist.py:close_position 写入的 closed record：
 *   open.{entry_date, actual_fill_price, position_size_shares}
 *   close.{exit_date, actual_exit_price, realized_pnl_amount, closed_at}
 * 其余字段宽松保留（diagnosis / open / close 里的扩展键）。
 */
export interface ClosedPosition {
  ts_code: string;
  name: string;
  open: {
    entry_date: string;
    entry_price: number | null;
    actual_fill_price: number | null;
    position_size_shares: number | null;
    [k: string]: unknown;
  };
  close: {
    exit_date: string;
    actual_exit_price: number;
    exit_reason: string;
    realized_pnl_amount: number | null;
    realized_pnl_pct: number | null;
    closed_at: string;
    [k: string]: unknown;
  };
  diagnosis: PostmortemDiagnosis | null;
  [k: string]: unknown;
}

/** GET /api/watchlist */
export async function getWatchlist(): Promise<WatchlistData> {
  return api.get<WatchlistData>("/watchlist");
}

/* ── Mutations(ED3 失效矩阵 SSOT 接入) ─────────────────────────── */

export interface AddCandidatePayload {
  ts_code: string;
  name: string;
  trigger_price: number;
  trigger_direction?: "below" | "above";
  stop_advice?: number;
  target_advice?: number;
  note?: string;
  strategy?: string;
  expires_days?: number;
  trigger_low?: number;
  trigger_high?: number;
  /** ADR-0001: 交易原型（可由 AI verdict setup_tag 预填） */
  setup?: string;
}

export interface ClosePositionPayload {
  ts_code: string;
  exit_price: number;
  exit_date: string; // YYYY-MM-DD
  exit_reason?: string;
  user_notes?: string;
  actual_fill_price?: number;
  postmortem?: boolean;
}

export interface ArchivePayload {
  ts_code: string;
  reason: "manual" | "expired" | "dedup";
  /** 缺省时后端自动探测(候选优先)。显式传更明确。 */
  section?: "active_positions" | "candidates";
}

export interface PromotePayload {
  ts_code: string;
  entry_price: number;
  /** 手数。键名必须对齐后端 PromoteCandidateRequest.position_size_shares
   *  (后端 extra=forbid, 错名直接 422 — 曾误发 shares 被静默吞掉, 500→ATR兜底6300) */
  position_size_shares: number;
  stop_loss: number;
  target: number;
  /** ADR-0001: 交易原型（确认/覆盖候选的 setup） */
  setup?: string;
  /** ADR-0001: 自录 Rule 检查表（承诺项，checked 入场时为 null，close 后评估） */
  rule_checklist?: RuleChecklist;
}

export interface AddPositionPayload {
  ts_code: string;
  name: string;
  entry_price: number;
  stop_loss: number;
  target: number;
  shares?: number;
  position_size_shares?: number;
}

/** POST /api/watchlist/candidates - 响应带过期归因 meta */
export async function addCandidate(
  payload: AddCandidatePayload,
): Promise<{ message: string; ts_code: string } & ExpiresMeta> {
  return api.post("/watchlist/candidates", payload);
}

/** POST /api/watchlist/positions - 409 DuplicatePositionError 抛 ApiError */
export async function addPosition(
  payload: AddPositionPayload,
): Promise<{ message: string; ts_code: string }> {
  return api.post("/watchlist/positions", payload);
}

/** POST /api/watchlist/positions/replace - 409 后用 */
export async function replacePosition(
  payload: AddPositionPayload,
): Promise<{ message: string; ts_code: string }> {
  return api.post("/watchlist/positions/replace", payload);
}

/** POST /api/watchlist/promote - 候选->持仓 */
export async function promoteCandidate(
  payload: PromotePayload,
): Promise<{ message: string; ts_code: string }> {
  return api.post("/watchlist/promote", payload);
}

/** POST /api/watchlist/close - 平仓 + 触发 postmortem diagnosis */
export async function closePosition(
  payload: ClosePositionPayload,
): Promise<{ message: string; record: unknown; diagnosis: PostmortemDiagnosis | string | null }> {
  return api.post("/watchlist/close", payload);
}

/** POST /api/watchlist/candidates/{ts_code}/renew - 续期已过期候选(不调 AI) */
export async function renewCandidate(
  ts_code: string,
): Promise<{ message: string; ts_code: string; renew_count: number } & ExpiresMeta> {
  return api.post(`/watchlist/candidates/${ts_code}/renew`);
}

/** POST /api/watchlist/candidates/{ts_code}/sync-ai - 同步最近 AI 分析到候选(三字段全覆盖) */
export async function syncCandidateAi(
  ts_code: string,
): Promise<{
  message: string;
  ts_code: string;
  trigger_price: number;
  stop_advice: number | null;
  target_advice: number | null;
  analyzed_at: string;
} & ExpiresMeta> {
  return api.post(`/watchlist/candidates/${ts_code}/sync-ai`);
}

/** POST /api/watchlist/archive */
export async function archiveEntry(
  payload: ArchivePayload,
): Promise<{ message: string; moved: boolean }> {
  return api.post("/watchlist/archive", payload);
}

/** POST /api/watchlist/buy */
export async function buy(payload: BuyPayload): Promise<BuyResponse> {
  return api.post("/watchlist/buy", payload);
}

/** POST /api/watchlist/sell - 减仓或卖光 */
export async function sell(payload: SellPayload): Promise<SellResponse> {
  return api.post("/watchlist/sell", payload);
}

/** POST /api/watchlist/positions/{ts_code}/advice - 更新止损/目标(覆盖) */
export interface UpdateAdvicePayload {
  ts_code: string;
  stop_loss?: number;
  target?: number;
  calibrated_confidence?: number;
}

export async function updateAdvice(
  ts_code: string,
  payload: Omit<UpdateAdvicePayload, "ts_code">,
): Promise<{ message: string; position: ActivePosition }> {
  return api.post(
    `/watchlist/positions/${encodeURIComponent(ts_code)}/advice`,
    { ts_code, ...payload },
  );
}

/** PATCH /api/watchlist/positions/{ts_code} - 交易参数部分覆盖(未传/空字段不动) */
export interface UpdatePositionPayload {
  name?: string;
  stop_loss?: number;
  target?: number;
  trigger_price?: number;
  trigger_direction?: "below" | "above";
  trigger_low?: number;
  trigger_high?: number;
  expires_at?: string; // YYYY-MM-DD
  calibrated_confidence?: number;
  strategy?: string;
  setup?: string;
}

/** PATCH /api/watchlist/candidates/{ts_code} - 交易参数部分覆盖(未传/空字段不动) */
export interface UpdateCandidatePayload {
  name?: string;
  trigger_price?: number;
  trigger_direction?: "below" | "above";
  trigger_low?: number;
  trigger_high?: number;
  stop_advice?: number;
  target_advice?: number;
  note?: string;
  expires_at?: string; // YYYY-MM-DD
  strategy?: string;
  setup?: string;
}

/** PATCH /api/watchlist/positions/{ts_code} - 编辑持仓交易参数(部分覆盖) */
export async function updatePosition(
  ts_code: string,
  payload: UpdatePositionPayload,
): Promise<{ message: string; position: ActivePosition }> {
  return api.patch(`/watchlist/positions/${encodeURIComponent(ts_code)}`, payload);
}

/** PATCH /api/watchlist/candidates/{ts_code} - 编辑候选交易参数(部分覆盖) */
export async function updateCandidate(
  ts_code: string,
  payload: UpdateCandidatePayload,
): Promise<{ message: string; candidate: Candidate }> {
  return api.patch(`/watchlist/candidates/${encodeURIComponent(ts_code)}`, payload);
}

/** GET /api/watchlist/trades - 交易流水(倒序) */
export async function getTrades(params?: {
  ts_code?: string;
  limit?: number;
  since_days?: number;
}): Promise<Trade[]> {
  const qs = new URLSearchParams();
  if (params?.ts_code) qs.set("ts_code", params.ts_code);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.since_days) qs.set("since_days", String(params.since_days));
  const q = qs.toString();
  return api.get<Trade[]>(`/watchlist/trades${q ? `?${q}` : ""}`);
}

/** GET /api/watchlist/closed - 已平仓记录（按 closed_at 倒序）。 */
export async function getClosedPositions(params?: {
  limit?: number;
  since_days?: number;
}): Promise<ClosedPosition[]> {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.since_days) qs.set("since_days", String(params.since_days));
  const q = qs.toString();
  return api.get<ClosedPosition[]>(`/watchlist/closed${q ? `?${q}` : ""}`);
}

// Re-export ApiError for callers
import { ApiError } from "./client";
export { ApiError };
