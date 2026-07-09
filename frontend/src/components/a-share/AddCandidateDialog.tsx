import { useEffect, useState } from "react";
import { Dialog, Button } from "@/components/base";
import { useAddCandidate } from "@/api/mutations";
import { cn } from "@/lib/utils";
import { SETUP_SEED } from "@/lib/trading-system";

/**
 * <AddCandidateDialog> — 把 AI 分析结果一键挂成候选
 *
 * 预填 verdict.price_advice: entry→trigger_price / stop_loss→stop_advice /
 * target→target_advice。用户确认 trigger_direction(回踩/突破) + 过期天数后提交。
 *
 * 字段形状以 apex/analyze.py:run 返回的嵌套 price_advice 为准(勿照 mock 平铺)。
 * trading flow: candidate → position, AI 给的 entry 价往往还没到, 挂候选等触发,
 * 到价成交后用 promote_candidate 以实际成交价转持仓。
 */

const inputCls =
  "num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none";
const labelCls = "mb-1 block text-xs text-text-secondary";

export function AddCandidateDialog({
  open,
  onClose,
  verdict,
}: {
  open: boolean;
  onClose: () => void;
  verdict: Record<string, unknown>;
}) {
  const addMut = useAddCandidate();
  const pa = (verdict.price_advice ?? {}) as Record<string, unknown>;
  const tsCode = String(verdict.ts_code ?? "").toUpperCase();

  const [triggerPrice, setTriggerPrice] = useState("");
  const [triggerLow, setTriggerLow] = useState("");
  const [triggerHigh, setTriggerHigh] = useState("");
  const [stopAdvice, setStopAdvice] = useState("");
  const [targetAdvice, setTargetAdvice] = useState("");
  const [direction, setDirection] = useState<"below" | "above">("below");
  const [expiresDays, setExpiresDays] = useState("7");
  const [note, setNote] = useState("");
  const [setup, setSetup] = useState("");
  const [setupCustom, setSetupCustom] = useState("");

  // 打开时按 verdict 预填(每次打开都重置, 避免上次残留)
  useEffect(() => {
    if (!open) return;
    const entry = pa.entry != null && pa.entry !== "" ? String(pa.entry) : "";
    setTriggerPrice(entry);
    setTriggerLow(pa.entry_low != null && pa.entry_low !== "" ? String(pa.entry_low) : "");
    setTriggerHigh(pa.entry_high != null && pa.entry_high !== "" ? String(pa.entry_high) : "");
    setStopAdvice(pa.stop_loss != null && pa.stop_loss !== "" ? String(pa.stop_loss) : "");
    setTargetAdvice(pa.target != null && pa.target !== "" ? String(pa.target) : "");
    setDirection("below");
    setExpiresDays("7");
    setNote(entry ? `AI分析 触发 ${entry}` : "AI分析 加入候选");
    // ADR-0001: setup 从 AI verdict 的 setup_tag 预填，种子词表内的直接选，其他走自定义
    const st = verdict.setup_tag;
    if (typeof st === "string" && st) {
      if ((SETUP_SEED as readonly string[]).includes(st)) {
        setSetup(st);
        setSetupCustom("");
      } else {
        setSetup("其他");
        setSetupCustom(st);
      }
    } else {
      setSetup("");
      setSetupCustom("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault();
    const tp = Number(triggerPrice);
    if (!tsCode || !tp) return;
    const low = triggerLow ? Number(triggerLow) : undefined;
    const high = triggerHigh ? Number(triggerHigh) : undefined;
    const finalSetup =
      setup === "其他"
        ? setupCustom.trim()
          ? `其他:${setupCustom.trim()}`
          : undefined
        : setup || undefined;
    addMut.mutate(
      {
        ts_code: tsCode,
        // verdict 不带 name, 传空让后端 _resolve_name 查 tushare 补真名;
        // 别传 ts_code 否则后端当已有名直接 return, 持仓只剩代码
        name: "",
        trigger_price: tp,
        trigger_direction: direction,
        stop_advice: stopAdvice ? Number(stopAdvice) : undefined,
        target_advice: targetAdvice ? Number(targetAdvice) : undefined,
        note,
        expires_days: Number(expiresDays) || 7,
        // 带状触发：low/high 都填才走区间, 否则退回单向阈值
        trigger_low: low,
        trigger_high: high,
        setup: finalSetup,
      },
      { onSuccess: () => onClose() },
    );
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={addMut.isPending}
      title={
        <span>
          加入候选 <span className="num text-text-secondary">{tsCode}</span>
        </span>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <p className="text-xs text-flat">
          AI 建议价位已预填, 确认触发方向与过期天数后挂为候选, 到价成交再用实际成交价转持仓。
        </p>

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelCls}>触发价 *</label>
            <input
              type="number"
              step="0.01"
              value={triggerPrice}
              onChange={(e) => setTriggerPrice(e.target.value)}
              placeholder="AI entry"
              className={inputCls}
              autoFocus
            />
          </div>
          <div>
            <label className={labelCls}>止损建议</label>
            <input
              type="number"
              step="0.01"
              value={stopAdvice}
              onChange={(e) => setStopAdvice(e.target.value)}
              placeholder="可选"
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>目标建议</label>
            <input
              type="number"
              step="0.01"
              value={targetAdvice}
              onChange={(e) => setTargetAdvice(e.target.value)}
              placeholder="可选"
              className={inputCls}
            />
          </div>
        </div>

        {/* 买入区间(带状触发): 都填才走区间, 避免接飞刀/追高; 留空退回单向阈值 */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls}>区间下沿</label>
            <input
              type="number"
              step="0.01"
              value={triggerLow}
              onChange={(e) => setTriggerLow(e.target.value)}
              placeholder="地板价"
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>区间上沿</label>
            <input
              type="number"
              step="0.01"
              value={triggerHigh}
              onChange={(e) => setTriggerHigh(e.target.value)}
              placeholder="天花板"
              className={inputCls}
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls}>触发方向</label>
            <div className="flex gap-1">
              {(["below", "above"] as const).map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => setDirection(d)}
                  className={cn(
                    "flex-1 rounded-md border px-2 py-1.5 text-xs",
                    direction === d
                      ? "border-text-primary bg-bg-base text-text-primary"
                      : "border-border text-text-secondary hover:bg-bg-base",
                  )}
                >
                  {d === "below" ? "回踩(below)" : "突破(above)"}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className={labelCls}>过期天数</label>
            <input
              type="number"
              step="1"
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
              className={inputCls}
            />
          </div>
        </div>

        <div>
          <label className={labelCls}>Setup（交易原型）</label>
          <div className="flex gap-2">
            <select
              value={setup}
              onChange={(e) => setSetup(e.target.value)}
              className="flex-1 rounded-md border border-border bg-bg-card px-2 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
            >
              <option value="">不标注</option>
              {SETUP_SEED.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
              <option value="其他">其他（自定义）</option>
            </select>
            {setup === "其他" && (
              <input
                type="text"
                value={setupCustom}
                onChange={(e) => setSetupCustom(e.target.value)}
                placeholder="自定义 setup"
                className="flex-1 rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none"
              />
            )}
          </div>
        </div>

        <div>
          <label className={labelCls}>备注</label>
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className={inputCls}
          />
        </div>

        {addMut.isError && (
          <p className="text-xs text-down">失败 · {String(addMut.error)}</p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={addMut.isPending}
          >
            取消
          </Button>
          <Button type="submit" variant="primary" disabled={addMut.isPending}>
            {addMut.isPending ? "加入中…" : "确认加入候选"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
