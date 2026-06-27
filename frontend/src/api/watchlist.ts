import { api } from "./client";

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
  stop_loss: number;
  target: number;
  trigger_price: number | null;
  trigger_direction: "below" | "above";
  expires_at: string;
  status: string;
  position_size_shares?: number;
  risk_amount?: number;
  calibrated_confidence?: number;
  strategy?: string;
  note?: string;
}

export interface Candidate {
  ts_code: string;
  name: string;
  trigger_price: number;
  trigger_direction: "below" | "above";
  expires_at: string;
  note: string;
  stop_advice: number;
  target_advice: number;
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
  diagnosis: unknown;
}

export type SellResponse = SellResponsePartial | SellResponseClosed;

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
}

export interface ClosePositionPayload {
  ts_code: string;
  exit_price: number;
  exit_date: string; // YYYY-MM-DD
  shares?: number;
  notes?: string;
}

export interface ArchivePayload {
  ts_code: string;
  reason: "manual" | "expired" | "dedup";
}

export interface PromotePayload {
  ts_code: string;
  entry_price: number;
  shares: number;
  stop_loss: number;
  target: number;
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

/** POST /api/watchlist/candidates */
export async function addCandidate(
  payload: AddCandidatePayload,
): Promise<{ message: string; ts_code: string }> {
  if (USE_MOCK) {
    MOCK_DATA.candidates.push({
      ts_code: payload.ts_code,
      name: payload.name,
      trigger_price: payload.trigger_price,
      trigger_direction: payload.trigger_direction ?? "below",
      expires_at: new Date(Date.now() + (payload.expires_days ?? 10) * 86400000)
        .toISOString()
        .slice(0, 10),
      note: payload.note ?? `手动加候选 触发 ${payload.trigger_price}`,
      stop_advice: payload.stop_advice ?? 0,
      target_advice: payload.target_advice ?? 0,
    });
    return { message: "Candidate added (mock)", ts_code: payload.ts_code };
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
): Promise<{ message: string; record: unknown; diagnosis: string }> {
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

// Re-export ApiError for callers
import { ApiError } from "./client";
export { ApiError };
