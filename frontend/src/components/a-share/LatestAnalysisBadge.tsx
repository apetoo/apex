import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { Drawer } from "@/components/base";
import { VerdictDetailCard, VerdictTag } from "@/components/a-share";
import { getLatestJournal, type LatestJournal } from "@/api/analyze";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import { verdictColor } from "@/types/verdict";

/**
 * <LatestAnalysisBadge> — 卡片上的「最近分析」徽标行 + hover 浮窗 + 点击 Drawer
 *
 * 数据: GET /api/journal/{ts_code}/latest(react-query 按 ts_code 缓存, staleTime 5min)。
 * 两种 kind 自动换措辞:
 *   - source === "position_action"(持仓常见): 动作 + 新止损(+方向箭头, 需 stopBefore)
 *   - 否则 verdict: 方向 + 校准置信度
 * 陈旧(>5 天)整行灰化 + 日期改显 "N 天前"。无 journal / loading -> 不渲染(零占位)。
 * hover 浮窗纯 CSS group-hover, 移动端无 hover 自然退化为点击 -> Drawer(复用 VerdictDetailCard)。
 */

/** 陈旧阈值(天)。 */
const STALE_DAYS = 5;

const ACTION_LABELS: Record<string, string> = {
  hold: "持有",
  add: "加仓",
  trim: "减仓",
  exit: "清仓",
};

function actionTone(action: string): string {
  if (action === "add") return "text-up";
  if (action === "trim" || action === "exit") return "text-down";
  return "text-text-primary";
}

/** 距今整天数; 解析失败返回 null。 */
function daysSince(iso?: string): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

export function LatestAnalysisBadge({
  tsCode,
  stopBefore,
}: {
  tsCode: string;
  /** 旧止损(持仓卡传 plan.last_stop_before ?? stop_loss), 用于 position_action 止损方向箭头。 */
  stopBefore?: number | null;
}) {
  const { data } = useQuery({
    queryKey: qk.journalLatest(tsCode),
    queryFn: () => getLatestJournal(tsCode),
    staleTime: 5 * 60_000,
  });
  const [drawerOpen, setDrawerOpen] = useState(false);

  if (!data) return null; // loading / 无记录 / 错误 -> 零占位

  const isPa = data.source === "position_action";
  const insufficient = data.analysis_status === "insufficient_evidence";
  const insufficientLabel = data.outcome_reason === "evidence_gap" || !data.outcome_reason
    ? "证据不足"
    : "分析未完成";
  const days = daysSince(data.analyzed_at);
  const stale = days != null && days > STALE_DAYS;
  const dateLabel =
    stale && days != null
      ? `${days} 天前`
      : (data.analyzed_at ?? "").slice(5, 10) || "-";

  return (
    <div className="group relative mt-1">
      {/* 徽标行(常驻, 点击开 Drawer) */}
      <button
        type="button"
        onClick={() => setDrawerOpen(true)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-[11px] hover:bg-bg-base",
          stale ? "text-flat" : "text-text-secondary",
        )}
        title="查看最近一次 AI 分析"
      >
        <Sparkles className="h-3 w-3 shrink-0 text-up" />
        {insufficient ? (
          <span className="font-medium text-flat">{insufficientLabel}</span>
        ) : isPa ? (
          <PaBadgeLine entry={data} stopBefore={stopBefore} />
        ) : (
          <VerdictBadgeLine entry={data} />
        )}
        <span className="num ml-auto shrink-0 opacity-60">{dateLabel}</span>
      </button>

      {/* hover 浮窗(桌面; 向上弹出避免遮下方卡片价格区) */}
      <div className="absolute bottom-full left-0 z-20 pb-1 hidden w-64 group-hover:block">
        <div
          className="cursor-pointer rounded-md border border-border bg-bg-card p-3 text-left shadow-lg"
          onClick={() => setDrawerOpen(true)}
        >
          {insufficient ? (
            <InsufficientPopover entry={data} />
          ) : isPa ? (
            <PaPopover entry={data} stopBefore={stopBefore} />
          ) : (
            <VerdictPopover entry={data} />
          )}
        </div>
      </div>

      {drawerOpen && (
        <Drawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          title="最近分析"
        >
          <VerdictDetailCard verdict={data as unknown as Record<string, unknown>} />
        </Drawer>
      )}
    </div>
  );
}

// ── 徽标行(单行) ────────────────────────────────────────────────────────────

function VerdictBadgeLine({ entry }: { entry: LatestJournal }) {
  const v = entry.verdict ?? "";
  const color = verdictColor(v);
  const tone =
    color === "up" ? "text-up" : color === "down" ? "text-down" : "text-flat";
  return (
    <>
      <span className={cn("font-medium", tone)}>{v || "—"}</span>
      {entry.calibrated_confidence != null && (
        <span className="opacity-80">校准 {entry.calibrated_confidence}</span>
      )}
      {entry.calibrated_confidence == null && entry.confidence != null && (
        <span className="opacity-80">置信 {entry.confidence}</span>
      )}
    </>
  );
}

function PaBadgeLine({
  entry,
  stopBefore,
}: {
  entry: LatestJournal;
  stopBefore?: number | null;
}) {
  const pa = entry.position_action ?? {};
  const action = pa.action ?? "hold";
  const label = ACTION_LABELS[action] ?? action;
  const newStop = pa.new_stop ?? null;
  const dir =
    newStop != null && stopBefore != null && newStop !== stopBefore
      ? newStop > stopBefore
        ? "↑收紧"
        : "↓放宽"
      : null;
  return (
    <>
      <span className={cn("font-medium", actionTone(action))}>{label}</span>
      {newStop != null && (
        <span className="num opacity-80">
          止损 {formatPrice(newStop)}
          {dir && (
            <span className={cn("ml-0.5", dir === "↑收紧" ? "text-up" : "text-down")}>
              {dir}
            </span>
          )}
        </span>
      )}
    </>
  );
}

// ── hover 浮窗 ──────────────────────────────────────────────────────────────

function VerdictPopover({ entry }: { entry: LatestJournal }) {
  const pa = entry.price_advice ?? {};
  const evidence = (entry.evidence ?? []).slice(0, 2);
  const hasEntry =
    (pa.entry_low != null && pa.entry_high != null) || pa.entry != null;
  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <VerdictTag verdict={entry.verdict} />
        <span className="num text-text-secondary">
          {entry.confidence != null && `置信${entry.confidence}`}
          {entry.confidence != null && entry.calibrated_confidence != null && " / "}
          {entry.calibrated_confidence != null && `校准${entry.calibrated_confidence}`}
        </span>
      </div>
      <div className="num space-y-0.5 text-text-secondary">
        {hasEntry && (
          <p>
            入场{" "}
            {pa.entry_low != null && pa.entry_high != null
              ? `${formatPrice(pa.entry_low)}–${formatPrice(pa.entry_high)}`
              : formatPrice(pa.entry as number)}
          </p>
        )}
        {(pa.stop_loss != null || pa.target != null) && (
          <p>
            {pa.stop_loss != null && (
              <span className="text-down">止损 {formatPrice(pa.stop_loss)}</span>
            )}
            {pa.stop_loss != null && pa.target != null && " · "}
            {pa.target != null && (
              <span className="text-up">目标 {formatPrice(pa.target)}</span>
            )}
          </p>
        )}
      </div>
      {evidence.length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-text-primary">
          {evidence.map((ev, i) => (
            <li key={i} className="flex items-start gap-1">
              <span className="num text-flat">{i + 1}.</span>
              <span className="line-clamp-2">{evidenceText(ev)}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="border-t border-border pt-1.5 text-flat">点击查看完整分析 →</p>
    </div>
  );
}

function evidenceText(ev: NonNullable<LatestJournal["evidence"]>[number]): string {
  if (typeof ev === "string") return ev;
  return ev.inference ? `${ev.fact} → ${ev.inference}` : ev.fact;
}

function InsufficientPopover({ entry }: { entry: LatestJournal }) {
  const title = entry.outcome_reason === "evidence_gap" || !entry.outcome_reason
    ? "证据不足，暂不判断"
    : "分析流程未完成，暂不判断";
  return (
    <div className="space-y-2 text-[11px]">
      <p className="font-semibold text-flat">{title}</p>
      {entry.research_summary && (
        <p className="text-text-secondary">{entry.research_summary}</p>
      )}
      {(entry.unknowns ?? []).length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-text-primary">
          {(entry.unknowns ?? []).slice(0, 2).map((unknown, index) => (
            <li key={index}>• {unknown}</li>
          ))}
        </ul>
      )}
      <p className="border-t border-border pt-1.5 text-flat">点击查看补证详情 →</p>
    </div>
  );
}

function PaPopover({
  entry,
  stopBefore,
}: {
  entry: LatestJournal;
  stopBefore?: number | null;
}) {
  const pa = entry.position_action ?? {};
  const action = pa.action ?? "hold";
  const label = ACTION_LABELS[action] ?? action;
  let sub = "";
  if (action === "add" && pa.add_shares != null) sub = `+${pa.add_shares}股`;
  else if (action === "trim" && pa.trim_shares != null) sub = `-${pa.trim_shares}股`;
  else if (action === "trim" && pa.trim_pct != null)
    sub = `-${Math.round(pa.trim_pct * 100)}%`;

  const newStop = pa.new_stop ?? null;
  const stopChanged = newStop != null && stopBefore != null && newStop !== stopBefore;
  const tighten = stopChanged && (newStop as number) > (stopBefore as number);
  const planCount = pa.scale_plan?.length ?? 0;

  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <span className={cn("text-sm font-semibold", actionTone(action))}>
          {label}
          {sub && <span className="num ml-1 text-xs font-normal">{sub}</span>}
        </span>
        <span className="num text-text-secondary">
          {(entry.analyzed_at ?? "").slice(5, 16).replace("T", " ")}
        </span>
      </div>
      <p className="num text-text-secondary">
        止损{" "}
        {stopChanged ? (
          <>
            {formatPrice(stopBefore as number)} → {formatPrice(newStop as number)}{" "}
            <span className={tighten ? "text-up" : "text-down"}>
              {tighten ? "↑收紧" : "↓放宽"}
            </span>
          </>
        ) : newStop != null ? (
          formatPrice(newStop)
        ) : (
          "维持"
        )}
      </p>
      {pa.rationale && (
        <div className="border-t border-border pt-2">
          <p className="mb-0.5 text-flat">核心判断</p>
          <p className="line-clamp-4 text-text-primary">{pa.rationale}</p>
        </div>
      )}
      <p className="border-t border-border pt-1.5 text-flat">
        {planCount > 0 ? `未来触发计划 ${planCount} 条 · ` : ""}点击查看完整分析 →
      </p>
    </div>
  );
}
