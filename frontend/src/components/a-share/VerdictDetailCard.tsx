import { useState } from "react";
import { Sparkles, Target, ShieldAlert, Flag, ListChecks } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Markdown, Button } from "@/components/base";
import { VerdictTag } from "./VerdictTag";
import { AddCandidateDialog } from "./AddCandidateDialog";
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
