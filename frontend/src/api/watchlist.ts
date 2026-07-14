import { api } from "./client";
import type { RuleChecklist } from "@/lib/trading-system";

/**
 * Watchlist API
 *
 * 对应 backend/routers/watchlist.py:
 *   GET    /api/watchlist               → { active_positions, candidates, archived }
 *   POST   /api/watchlist/positions     → 加持仓
 *   POST   /api/watchlist/positions/replace → 替换重复持仓(409 后用)
 *   POST   /api/watchlist/candidates    → 加候选
 *   POST   /api/watchlist/promote       → 候选→持仓(实际成交)
 *   POST   /api/watchlist/close         → 平仓(返回 diagnosis)
 *   POST   /api/watchlist/archive       → 归档
 *   GET    /api/watchlist/closed        → 已平仓
 *   POST   /api/watchlist/migrate       → 一次性迁移
 *
 * 字段定义见 apex/watchlist.py(单 user, 文件存储)。
 * ED3 失效矩阵的失效目标 = ['watchlist', 'account', 'triggers', 'closed', 'calibration']
 *
 * PR1b: dev mock 模式联调(后端大概率没跑)。
 */

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

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
  note?: string;
  /** ADR-0001: 交易原型（candidate 继承或 promote 时确认） */
  setup?: string;
  /** ADR-0001: 自录 Rule 检查表，close 后守规算分 */
  rule_checklist?: RuleChecklist;
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
 * 平仓复盘 diagnosis（apex/postmortem.py:run_and_patch 返回的 dict，非字符串）。
 * 机械字段由代码算好（outcome_class / nearly_hit_target / stop_hit_too_tight /
 * calibration_bucket / target_distance_max_pct），AI 只填因果解释。
 * **注意**: dev mock 的 closePosition 返回字符串 diagnosis，真后端返回此对象 --
 * 渲染端必须兼容两者（见 WatchlistPage 平仓弹窗）。
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

const MOCK_DATA: WatchlistData = {
  active_positions: [
    {
      ts_code: "002466.SZ",
      name: "天齐锂业",
      entry_price: 66.04,
      entry_date: "2026-06-24",
      stop_loss: 60.71,
      target: 72.0,
      trigger_price: null,
      trigger_direction: "below",
      expires_at: "2026-07-04",
      status: "active",
      position_size_shares: 100,
      risk_amount: 533,
      strategy: "analyze",
    },
  ],
  candidates: [
    {
      ts_code: "002415.SZ",
      name: "海康威视",
      trigger_price: 31.5,
      trigger_direction: "below",
      expires_at: "2026-06-30",
      note: "AI建议买入 31.5(止损 30.16 目标 35)",
      stop_advice: 30.16,
      target_advice: 35,
    },
    {
      ts_code: "002475.SZ",
      name: "立讯精密",
      trigger_price: 66.0,
      trigger_direction: "below",
      expires_at: "2026-06-30",
      note: "AI偏多但未给价位, 手动设触发 66.0",
      stop_advice: 0,
      target_advice: 0,
    },
  ],
  archived: [],
};

/** GET /api/watchlist */
export async function getWatchlist(): Promise<WatchlistData> {
  if (USE_MOCK) return MOCK_DATA;
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
  shares: number;
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

/** POST /api/watchlist/candidates — 响应带过期归因 meta */
export async function addCandidate(
  payload: AddCandidatePayload,
): Promise<{ message: string; ts_code: string } & ExpiresMeta> {
  if (USE_MOCK) {
    // 对齐真后端: expires_days 不传(undefined)→ mock 动态默认 7; 显式传→manual
    const isManual = payload.expires_days != null;
    const days = payload.expires_days ?? 7;
    const meta: ExpiresMeta = isManual
      ? { expires_days: days, method: "manual" }
      : { expires_days: days, method: "vol_based", realized_vol: 0.03, distance_pct: 5.2, t_trading: 2.99 };
    MOCK_DATA.candidates.push({
      ts_code: payload.ts_code,
      name: payload.name,
      trigger_price: payload.trigger_price,
      trigger_direction: payload.trigger_direction ?? "below",
      expires_at: new Date(Date.now() + days * 86400000).toISOString().slice(0, 10),
      expires_meta: meta,
      note: payload.note ?? `手动加候选 触发 ${payload.trigger_price}`,
      stop_advice: payload.stop_advice ?? 0,
      target_advice: payload.target_advice ?? 0,
      ...(payload.setup ? { setup: payload.setup } : {}),
      ...(payload.trigger_low != null ? { trigger_low: payload.trigger_low } : {}),
      ...(payload.trigger_high != null ? { trigger_high: payload.trigger_high } : {}),
    });
    return { message: "Candidate added (mock)", ts_code: payload.ts_code, ...meta };
  }
  return api.post("/watchlist/candidates", payload);
}

/** POST /api/watchlist/positions — 409 DuplicatePositionError 抛 ApiError */
export async function addPosition(
  payload: AddPositionPayload,
): Promise<{ message: string; ts_code: string }> {
  if (USE_MOCK) {
    if (MOCK_DATA.active_positions.some((p) => p.ts_code === payload.ts_code)) {
      throw new ApiError(409, `已存在 ${payload.ts_code} 持仓`, {
        ts_code: payload.ts_code,
        existing: MOCK_DATA.active_positions.find(
          (p) => p.ts_code === payload.ts_code,
        ),
      });
    }
    MOCK_DATA.active_positions.push({
      ts_code: payload.ts_code,
      name: payload.name,
      entry_price: payload.entry_price,
      entry_date: new Date().toISOString().slice(0, 10),
      stop_loss: payload.stop_loss,
      target: payload.target,
      trigger_price: null,
      trigger_direction: "below",
      expires_at: new Date(Date.now() + 10 * 86400000)
        .toISOString()
        .slice(0, 10),
      status: "active",
      position_size_shares: payload.position_size_shares ?? payload.shares,
      risk_amount:
        payload.position_size_shares && payload.entry_price
          ? Math.abs(payload.entry_price - payload.stop_loss) *
            payload.position_size_shares
          : undefined,
    });
    return { message: "Position added (mock)", ts_code: payload.ts_code };
  }
  return api.post("/watchlist/positions", payload);
}

/** POST /api/watchlist/positions/replace — 409 后用 */
export async function replacePosition(
  payload: AddPositionPayload,
): Promise<{ message: string; ts_code: string }> {
  if (USE_MOCK) {
    const i = MOCK_DATA.active_positions.findIndex(
      (p) => p.ts_code === payload.ts_code,
    );
    if (i >= 0) MOCK_DATA.active_positions.splice(i, 1);
    MOCK_DATA.active_positions.push({
      ts_code: payload.ts_code,
      name: payload.name,
      entry_price: payload.entry_price,
      entry_date: new Date().toISOString().slice(0, 10),
      stop_loss: payload.stop_loss,
      target: payload.target,
      trigger_price: null,
      trigger_direction: "below",
      expires_at: new Date(Date.now() + 10 * 86400000)
        .toISOString()
        .slice(0, 10),
      status: "active",
      position_size_shares: payload.position_size_shares ?? payload.shares,
    });
    return { message: "Replaced (mock)", ts_code: payload.ts_code };
  }
  return api.post("/watchlist/positions/replace", payload);
}

/** POST /api/watchlist/promote — 候选→持仓 */
export async function promoteCandidate(
  payload: PromotePayload,
): Promise<{ message: string; ts_code: string }> {
  if (USE_MOCK) {
    const c = MOCK_DATA.candidates.find((x) => x.ts_code === payload.ts_code);
    if (c) {
      MOCK_DATA.active_positions.push({
        ts_code: c.ts_code,
        name: c.name,
        entry_price: payload.entry_price,
        entry_date: new Date().toISOString().slice(0, 10),
        stop_loss: payload.stop_loss,
        target: payload.target,
        trigger_price: null,
        trigger_direction: "below",
        expires_at: new Date(Date.now() + 10 * 86400000)
          .toISOString()
          .slice(0, 10),
        status: "active",
        position_size_shares: payload.shares,
        ...(payload.setup ? { setup: payload.setup } : {}),
        ...(payload.rule_checklist ? { rule_checklist: payload.rule_checklist } : {}),
      });
      MOCK_DATA.archived.push({
        ...c,
        status: "archived_promoted",
        archived_at: new Date().toISOString(),
      });
      MOCK_DATA.candidates = MOCK_DATA.candidates.filter(
        (x) => x.ts_code !== payload.ts_code,
      );
    }
    return { message: "Promoted (mock)", ts_code: payload.ts_code };
  }
  return api.post("/watchlist/promote", payload);
}

/** POST /api/watchlist/close — 平仓 + 触发 postmortem diagnosis */
export async function closePosition(
  payload: ClosePositionPayload,
): Promise<{ message: string; record: unknown; diagnosis: PostmortemDiagnosis | string | null }> {
  if (USE_MOCK) {
    const i = MOCK_DATA.active_positions.findIndex(
      (p) => p.ts_code === payload.ts_code,
    );
    const closed = i >= 0 ? MOCK_DATA.active_positions.splice(i, 1)[0] : null;
    const record = {
      ...(closed ?? { ts_code: payload.ts_code }),
      exit_price: payload.exit_price,
      exit_date: payload.exit_date,
      net_return:
        closed && closed.entry_price
          ? (payload.exit_price - closed.entry_price) / closed.entry_price
          : 0,
    };
    return {
      message: "Closed (mock)",
      record,
      diagnosis: `AI 复盘: ${payload.ts_code} 已平仓, 收益 ${
        record.net_return >= 0 ? "+" : ""
      }${(record.net_return * 100).toFixed(2)}%。回顾入场逻辑, 检查 ${closed?.note ?? "AI verdict"} 决策依据。`,
    };
  }
  return api.post("/watchlist/close", payload);
}

/** POST /api/watchlist/archive */
/** POST /api/watchlist/candidates/{ts_code}/renew — 续期已过期候选(不调 AI) */
export async function renewCandidate(
  ts_code: string,
): Promise<{ message: string; ts_code: string; renew_count: number } & ExpiresMeta> {
  if (USE_MOCK) {
    const c = MOCK_DATA.candidates.find((x) => x.ts_code === ts_code);
    if (!c) throw new Error(`候选 ${ts_code} 不存在`);
    const days = 7;
    const meta: ExpiresMeta = {
      expires_days: days,
      method: "vol_based",
      realized_vol: 0.03,
      distance_pct: 5.2,
      t_trading: 2.99,
    };
    c.expires_at = new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
    c.expires_meta = meta;
    c.renew_count = (c.renew_count ?? 0) + 1;
    return { message: "Candidate renewed (mock)", ts_code, renew_count: c.renew_count, ...meta };
  }
  return api.post(`/watchlist/candidates/${ts_code}/renew`);
}

/** POST /api/watchlist/candidates/{ts_code}/sync-ai — 同步最近 AI 分析到候选(三字段全覆盖) */
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
  if (USE_MOCK) {
    const c = MOCK_DATA.candidates.find((x) => x.ts_code === ts_code);
    if (!c) throw new Error(`候选 ${ts_code} 不存在`);
    c.trigger_price = 68.7;
    c.stop_advice = 65.5;
    c.target_advice = 76;
    const days = 7;
    const meta: ExpiresMeta = { expires_days: days, method: "vol_based", realized_vol: 0.04, distance_pct: 3, t_trading: 0.56 };
    c.expires_at = new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
    c.expires_meta = meta;
    return { message: "Candidate synced (mock)", ts_code, trigger_price: 68.7, stop_advice: 65.5, target_advice: 76, analyzed_at: "2026-07-01T09:53:52+08:00", ...meta };
  }
  return api.post(`/watchlist/candidates/${ts_code}/sync-ai`);
}

export async function archiveEntry(
  payload: ArchivePayload,
): Promise<{ message: string; moved: boolean }> {
  if (USE_MOCK) {
    let moved = false;
    const i = MOCK_DATA.candidates.findIndex(
      (c) => c.ts_code === payload.ts_code,
    );
    if (i >= 0) {
      const [c] = MOCK_DATA.candidates.splice(i, 1);
      MOCK_DATA.archived.push({
        ...c,
        status: `archived_${payload.reason}`,
        archived_at: new Date().toISOString(),
      });
      moved = true;
    }
    return { message: moved ? "Entry archived (mock)" : "Entry not found", moved };
  }
  return api.post("/watchlist/archive", payload);
}

/** POST /api/watchlist/buy */
export async function buy(payload: BuyPayload): Promise<BuyResponse> {
  return api.post("/watchlist/buy", payload);
}

/** POST /api/watchlist/sell — 减仓或卖光 */
export async function sell(payload: SellPayload): Promise<SellResponse> {
  return api.post("/watchlist/sell", payload);
}

/** POST /api/watchlist/positions/{ts_code}/advice — 更新止损/目标(覆盖) */
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

/** GET /api/watchlist/trades — 交易流水(倒序) */
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

/** GET /api/watchlist/closed — 已平仓记录（按 closed_at 倒序）。 */
export async function getClosedPositions(params?: {
  limit?: number;
  since_days?: number;
}): Promise<ClosedPosition[]> {
  if (USE_MOCK) return [];
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.since_days) qs.set("since_days", String(params.since_days));
  const q = qs.toString();
  return api.get<ClosedPosition[]>(`/watchlist/closed${q ? `?${q}` : ""}`);
}

// Re-export ApiError for callers
import { ApiError } from "./client";
export { ApiError };
