import { useState } from "react";
import { Sparkles, Target, ShieldAlert, Flag, ListChecks, Gauge, Trophy } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Markdown, Button } from "@/components/base";
import { VerdictTag } from "./VerdictTag";
import { AddCandidateDialog } from "./AddCandidateDialog";
import { ScalePlanLadder } from "./ScalePlanLadder";
import type { ScalePlanItem } from "@/api/watchlist";
import { cn, formatPrice } from "@/lib/utils";

/**
 * <VerdictDetailCard> — 分析结果详情卡(共享)
 *
 * 复用于: /analyze 当前分析落地 + /journal 历史详情抽屉。
 * verdict 字段形状(后端 apex/analyze.py:run 返回的 entry):
 *   { ts_code, analyzed_at, verdict, confidence, calibrated_confidence,
 *     calibration_explanation, price_advice: { entry, stop_loss, target, ... },
 *     evidence: string[], analysis_text, ... }
 */

export function fmtPrice(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? formatPrice(n) : String(v);
}

export function VerdictDetailCard({ verdict }: { verdict: Record<string, unknown> }) {
  const [addOpen, setAddOpen] = useState(false);
  // v1.1.0: position_action 走专属持仓建议卡（当前决策 + 条件触发计划），不渲染 verdict 三宫格。
  // useState 提前无条件调用，避免 held/unheld 切换时 hooks 数量变化（同组件实例跨分支）。
  if (verdict.source === "position_action") {
    return <PositionActionDetailCard verdict={verdict} />;
  }
  const pa = (verdict.price_advice ?? {}) as Record<string, unknown>;
  const evidence = Array.isArray(verdict.evidence) ? (verdict.evidence as unknown[]) : [];
  const analysisText = typeof verdict.analysis_text === "string" ? verdict.analysis_text : "";
  const calibrated = verdict.calibrated_confidence;
  const calExplanation =
    typeof verdict.calibration_explanation === "string" ? verdict.calibration_explanation : "";
  // 24h 重复分析限幅标记（痛点#3透明化）。老 entry 无此字段，?. 兼容。
  const ra = (verdict.repeat_analysis ?? {}) as Record<string, unknown>;
  const limited = ra.limited === true;
  const isRepeat = ra.is_repeat === true;
  const limitTooltip = limited
    ? `AI 原始 ${String(ra.raw_confidence ?? "-")}(${String(ra.raw_verdict ?? "-")}) -> 限幅 ${String(verdict.confidence ?? "-")}(${String(verdict.verdict ?? "-")})${ra.limit_rule ? ` · ${String(ra.limit_rule)}` : ""}`
    : "";

  // Playstyle Engine v1（玩法星级 / risk_level / 契合度徽章）。老 entry 全 null -> 不渲染。
  // skill 分支：playstyle = {ratings, primary, secondary, reasons, method, low_confidence}。
  const playstyle = verdict.playstyle as
    | { ratings?: Record<string, number>; primary?: string; secondary?: string; reasons?: string[]; low_confidence?: boolean }
    | null;
  const playstyleFit = verdict.playstyle_fit as { state?: string; note?: string } | null;
  const riskLevel = typeof verdict.risk_level === "string" ? (verdict.risk_level as string) : null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-up" />
          <CardTitle>分析结果</CardTitle>
        </div>
        <div className="flex items-center gap-2">
          <VerdictTag verdict={String(verdict.verdict ?? "")} />
          <span className="num text-xs text-text-secondary">
            置信度 {String(verdict.confidence ?? "—")}
            {calibrated !== undefined && calibrated !== null && (
              <span className="text-flat"> / 校准 {String(calibrated)}</span>
            )}
          </span>
          {limited && (
            <span
              className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-flat"
              title={limitTooltip}
            >
              🔄 已限幅
            </span>
          )}
          {!limited && isRepeat && (
            <span
              className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-flat"
              title="24h 内重复分析，有新增信息未限幅"
            >
              🔄 24h 重复
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        {/* 价位建议: 入场有区间就显示带, 否则回退单值(老 entry 兼容) */}
        <div className="grid grid-cols-3 gap-3">
          <PriceCell
            icon={Target}
            label="入场"
            value={
              pa.entry_low != null && pa.entry_high != null
                ? `${fmtPrice(pa.entry_low)}–${fmtPrice(pa.entry_high)}`
                : fmtPrice(pa.entry)
            }
            tone="text-text-primary"
          />
          <PriceCell icon={ShieldAlert} label="止损" value={fmtPrice(pa.stop_loss)} tone="text-down" />
          <PriceCell icon={Flag} label="目标" value={fmtPrice(pa.target)} tone="text-up" />
        </div>

        {/* 玩法判定 (Playstyle Engine v1)：星级条 + risk_level + 契合度徽章。老 entry 不渲染。 */}
        <PlaystyleSection
          playstyle={playstyle}
          playstyleFit={playstyleFit}
          riskLevel={riskLevel}
        />

        {/* 证据链 */}
        {evidence.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-text-secondary">
              <ListChecks className="h-3.5 w-3.5" />
              证据链
            </div>
            <ul className="space-y-1">
              {evidence.map((ev, i) => (
                <li key={i} className="flex items-start gap-2 text-sm">
                  <span className="num mt-0.5 text-[10px] text-flat">{i + 1}.</span>
                  <span className="text-text-primary">{String(ev)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* AI 叙述 */}
        {analysisText && (
          <div>
            <div className="mb-1.5 text-xs font-medium text-text-secondary">AI 分析</div>
            <Markdown>{analysisText}</Markdown>
          </div>
        )}

        {/* 校准说明 */}
        {calExplanation && (
          <p className="rounded-md bg-bg-base p-2 text-[11px] text-flat">
            校准: {calExplanation}
          </p>
        )}

        {/* 加入候选: AI entry 价往往还没到, 挂候选等触发(见 trading flow) */}
        {pa.entry != null && pa.entry !== "" && (
          <div className="flex justify-end pt-1">
            <Button variant="primary" size="sm" onClick={() => setAddOpen(true)}>
              加入候选
            </Button>
          </div>
        )}

        <AddCandidateDialog open={addOpen} onClose={() => setAddOpen(false)} verdict={verdict} />
      </CardContent>
    </Card>
  );
}

function PriceCell({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Target;
  label: string;
  value: string;
  tone: string;
}) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className="flex items-center gap-1.5 text-xs text-text-secondary">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <p className={cn("num mt-1 text-lg font-semibold", tone)}>{value}</p>
    </div>
  );
}

// ── Playstyle Engine v1 ──────────────────────────────────────────────────────
// 玩法星级(打野/波段/中线/长线 0-5) + risk_level(low/medium/high) + 契合度徽章。
// v1 prototype-first(D9)：ratings 由 Python 规则 post-hoc 给出；契合度恒 insufficient_data(D10)。
// 雪球风克制：星级用文字星(不整块染色)，top 仅加粗+Trophy 标，risk high 用 amber(沿用续期警示色)。
const PLAYSTYLES = ["打野", "波段", "中线", "长线"] as const;

function StarBar({ rating }: { rating: number }) {
  const r = Math.max(0, Math.min(5, Math.round(Number(rating) || 0)));
  return (
    <span className="num tracking-tight text-text-primary">
      {"★".repeat(r)}
      <span className="text-flat">{"☆".repeat(5 - r)}</span>
    </span>
  );
}

function RiskBadge({ level }: { level: string }) {
  const isHigh = level === "high";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5",
        isHigh
          ? "text-amber-600 border-amber-600/30 bg-amber-600/10"
          : "text-flat border-flat/20 bg-flat/8",
      )}
    >
      {isHigh && <ShieldAlert className="h-3 w-3" />}
      风险{level === "high" ? "高" : level === "low" ? "低" : "中"}
    </span>
  );
}

function FitBadge({ fit }: { fit: { state?: string; note?: string } }) {
  // v1 恒 insufficient_data（D10：画像 n<20 即死，规则 v1.1）
  const label = fit.state === "insufficient_data" ? (fit.note ?? "画像累积中") : (fit.note ?? fit.state ?? "-");
  return (
    <span className="inline-flex items-center rounded border border-flat/20 bg-flat/8 px-1.5 py-0.5 text-flat">
      契合 {label}
    </span>
  );
}

function PlaystyleSection({
  playstyle,
  playstyleFit,
  riskLevel,
}: {
  playstyle: { ratings?: Record<string, number>; top?: string; reasons?: string[]; low_confidence?: boolean } | null;
  playstyleFit: { state?: string; note?: string } | null;
  riskLevel: string | null;
}) {
  // 老 entry(playstyle/fit/risk 全 null) -> 不渲染
  if (!playstyle && !playstyleFit && !riskLevel) return null;

  const ratings = playstyle?.ratings;
  const primary = playstyle?.primary;
  const secondary = playstyle?.secondary;
  const reasons = Array.isArray(playstyle?.reasons) ? (playstyle!.reasons as string[]) : [];

  return (
    <div className="rounded-md border border-border p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-xs font-medium text-text-secondary">
          <Gauge className="h-3.5 w-3.5" />
          玩法判定
        </div>
        <div className="flex items-center gap-1.5 text-[10px]">
          {riskLevel && <RiskBadge level={riskLevel} />}
          {playstyleFit && <FitBadge fit={playstyleFit} />}
        </div>
      </div>

      {ratings ? (
        <>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            {PLAYSTYLES.map((name) => {
              const isPrimary = name === primary;
              const isSecondary = name === secondary;
              return (
                <div key={name} className="flex items-center gap-2 text-sm">
                  <span
                    className={cn(
                      "w-8 shrink-0",
                      isPrimary ? "font-semibold text-text-primary" : "text-text-secondary",
                    )}
                  >
                    {name}
                  </span>
                  <StarBar rating={ratings[name] ?? 0} />
                  {isPrimary && (
                    <span className="inline-flex items-center gap-0.5 text-[10px] text-text-primary">
                      <Trophy className="h-3 w-3" />
                      主玩法
                    </span>
                  )}
                  {!isPrimary && isSecondary && (
                    <span className="text-[10px] text-text-secondary">兼容</span>
                  )}
                </div>
              );
            })}
          </div>
          {reasons.length > 0 && (
            <p className="mt-2 text-[11px] text-text-secondary">
              <span className="text-flat">原因 · </span>
              {reasons.join(" / ")}
            </p>
          )}
          {playstyle?.low_confidence && (
            <p className="mt-1 text-[10px] text-flat">各玩法特征命中弱，判定仅供参考</p>
          )}
        </>
      ) : (
        <p className="text-sm text-flat">
          玩法特征不足，未判定{playstyleFit?.note ? `（${playstyleFit.note}）` : ""}
        </p>
      )}
    </div>
  );
}

// ── v1.1.0: 持仓建议卡（source=position_action 的 journal entry 专用） ──────────
// 与 VerdictDetailCard 区别：当前决策(action) + 条件触发计划(scale_plan) + 判断理由(rationale)，
// 无入场/目标/证据链（verdict 字段全 None）。playstyle 仍渲染（股票客观属性，从最近 verdict 继承）。
// "现在 vs 未来"：当前建议=现在做什么；条件触发计划=价格触及才执行的 ladder。
const ACTION_META: Record<string, { label: string; tone: string; sub: string }> = {
  hold: { label: "持有", tone: "text-text-primary", sub: "不加不减" },
  add: { label: "加仓", tone: "text-up", sub: "" },
  trim: { label: "减仓", tone: "text-down", sub: "" },
  exit: { label: "清仓", tone: "text-down", sub: "全部离场" },
};

function PositionActionDetailCard({ verdict }: { verdict: Record<string, unknown> }) {
  const pa = (verdict.position_action ?? {}) as {
    action?: string;
    add_shares?: number | null;
    trim_shares?: number | null;
    trim_pct?: number | null;
    new_stop?: number | null;
    scale_plan?: ScalePlanItem[];
    rationale?: string;
  };
  const analysisText = typeof verdict.analysis_text === "string" ? verdict.analysis_text : "";
  const playstyle = verdict.playstyle as
    | { ratings?: Record<string, number>; primary?: string; secondary?: string; reasons?: string[]; low_confidence?: boolean }
    | null;
  const playstyleFit = verdict.playstyle_fit as { state?: string; note?: string } | null;
  const riskLevel = typeof verdict.risk_level === "string" ? (verdict.risk_level as string) : null;

  const action = pa.action ?? "hold";
  const meta = ACTION_META[action] ?? ACTION_META.hold;
  let sub = meta.sub;
  if (!sub) {
    if (action === "add" && pa.add_shares != null) sub = `+${pa.add_shares}股`;
    else if (action === "trim" && pa.trim_shares != null) sub = `-${pa.trim_shares}股`;
    else if (action === "trim" && pa.trim_pct != null) sub = `-${Math.round(pa.trim_pct * 100)}%`;
  }
  const analyzedAt = typeof verdict.analyzed_at === "string" ? (verdict.analyzed_at as string).slice(5, 16) : "";

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-up" />
          <CardTitle>持仓建议</CardTitle>
        </div>
        <div className="flex items-center gap-2">
          <span className={cn("rounded px-1.5 py-0.5 text-xs font-medium", meta.tone)}>{meta.label}</span>
          {analyzedAt && <span className="num text-xs text-text-secondary">{analyzedAt}</span>}
        </div>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        {/* 当前决策（现在）：action + 新止损 */}
        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-md border border-border p-3">
            <div className="text-xs text-text-secondary">当前建议（现在）</div>
            <p className={cn("num mt-1 text-lg font-semibold", meta.tone)}>{meta.label}</p>
            {sub && <p className="text-[10px] text-flat">{sub}</p>}
          </div>
          <div className="rounded-md border border-border p-3">
            <div className="text-xs text-text-secondary">新止损</div>
            <p className={cn("num mt-1 text-lg font-semibold", pa.new_stop != null ? "text-down" : "text-flat")}>
              {pa.new_stop != null ? fmtPrice(pa.new_stop) : "维持"}
            </p>
          </div>
        </div>

        {/* 玩法判定（从最近 verdict 继承的股票客观属性，持仓期稳定） */}
        <PlaystyleSection playstyle={playstyle} playstyleFit={playstyleFit} riskLevel={riskLevel} />

        {/* 条件触发计划（未来）：价格触及才执行，非现役指令 */}
        {pa.scale_plan && pa.scale_plan.length > 0 ? (
          <ScalePlanLadder items={pa.scale_plan} title="条件触发计划（价格触及才执行）" showReason />
        ) : null}

        {/* 判断理由（机器可读摘要） */}
        {pa.rationale && (
          <div>
            <div className="mb-1.5 text-xs font-medium text-text-secondary">判断理由</div>
            <Markdown>{pa.rationale}</Markdown>
          </div>
        )}

        {/* AI 叙述全文 */}
        {analysisText && (
          <div>
            <div className="mb-1.5 text-xs font-medium text-text-secondary">AI 分析</div>
            <Markdown>{analysisText}</Markdown>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
