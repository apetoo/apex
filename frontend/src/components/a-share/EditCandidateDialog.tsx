import { useEffect, useState } from "react";
import { Dialog, Button } from "@/components/base";
import { useUpdateCandidate } from "@/api/mutations";
import { SETUP_SEED } from "@/lib/trading-system";
import { cn } from "@/lib/utils";
import type { Candidate } from "@/api/watchlist";

/**
 * <EditCandidateDialog> - 人工编辑候选交易参数
 *
 * PATCH 部分覆盖: 空字段提交 undefined -> 后端不动。
 * 改 trigger_price 不重算过期(手动编辑=精确控制; 续期用 renew, 跟AI用 sync-ai)。
 *
 * 字段形状以 apex/watchlist.py:add_candidate 写入的 entry 为准。
 */
const inputCls =
  "num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none";
const labelCls = "mb-1 block text-xs text-text-secondary";

export function EditCandidateDialog({
  open,
  onClose,
  candidate,
}: {
  open: boolean;
  onClose: () => void;
  candidate: Candidate | null;
}) {
  const mut = useUpdateCandidate();

  const [name, setName] = useState("");
  const [triggerPrice, setTriggerPrice] = useState("");
  const [direction, setDirection] = useState<"below" | "above">("below");
  const [triggerLow, setTriggerLow] = useState("");
  const [triggerHigh, setTriggerHigh] = useState("");
  const [stopAdvice, setStopAdvice] = useState("");
  const [targetAdvice, setTargetAdvice] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [note, setNote] = useState("");
  const [strategy, setStrategy] = useState("");
  const [setup, setSetup] = useState("");
  const [setupCustom, setSetupCustom] = useState("");

  // 打开时按当前候选预填(每次打开都重置, 避免上次残留)
  useEffect(() => {
    if (!open || !candidate) return;
    setName(candidate.name ?? "");
    setTriggerPrice(
      candidate.trigger_price != null ? String(candidate.trigger_price) : "",
    );
    setDirection(candidate.trigger_direction === "above" ? "above" : "below");
    setTriggerLow(candidate.trigger_low != null ? String(candidate.trigger_low) : "");
    setTriggerHigh(candidate.trigger_high != null ? String(candidate.trigger_high) : "");
    setStopAdvice(candidate.stop_advice ? String(candidate.stop_advice) : "");
    setTargetAdvice(candidate.target_advice ? String(candidate.target_advice) : "");
    setExpiresAt(candidate.expires_at ?? "");
    setNote(candidate.note ?? "");
    setStrategy(candidate.strategy ?? "");
    const st = candidate.setup;
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
  }, [open, candidate]);

  if (!candidate) return null;
  const tsCode = candidate.ts_code;

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault();
    const finalSetup =
      setup === "其他"
        ? setupCustom.trim()
          ? `其他:${setupCustom.trim()}`
          : undefined
        : setup || undefined;
    mut.mutate(
      {
        ts_code: tsCode,
        name: name.trim() || undefined,
        trigger_price: triggerPrice ? Number(triggerPrice) : undefined,
        trigger_direction: direction,
        trigger_low: triggerLow ? Number(triggerLow) : undefined,
        trigger_high: triggerHigh ? Number(triggerHigh) : undefined,
        stop_advice: stopAdvice ? Number(stopAdvice) : undefined,
        target_advice: targetAdvice ? Number(targetAdvice) : undefined,
        expires_at: expiresAt || undefined,
        note: note.trim() || undefined,
        strategy: strategy.trim() || undefined,
        setup: finalSetup,
      },
      { onSuccess: () => onClose() },
    );
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={mut.isPending}
      title={
        <span>
          编辑候选 <span className="num text-text-secondary">{tsCode}</span>
        </span>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <p className="text-xs text-flat">
          人工干预修改候选参数。空字段不传(保持原值)。改触发价不会重算过期。
        </p>

        {/* 触发价 / 止损建议 / 目标建议 */}
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelCls}>触发价</label>
            <input
              type="number"
              step="0.01"
              value={triggerPrice}
              onChange={(e) => setTriggerPrice(e.target.value)}
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>止损建议</label>
            <input
              type="number"
              step="0.01"
              value={stopAdvice}
              onChange={(e) => setStopAdvice(e.target.value)}
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
              className={inputCls}
            />
          </div>
        </div>

        {/* 区间下沿 / 区间上沿 */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls}>区间下沿</label>
            <input
              type="number"
              step="0.01"
              value={triggerLow}
              onChange={(e) => setTriggerLow(e.target.value)}
              placeholder="带状触发"
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
              placeholder="带状触发"
              className={inputCls}
            />
          </div>
        </div>

        {/* 触发方向 / 过期日期 */}
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
            <label className={labelCls}>过期日期</label>
            <input
              type="date"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
              className={inputCls}
            />
          </div>
        </div>

        {/* 名称 / Strategy */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls}>名称</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>Strategy</label>
            <input
              type="text"
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              placeholder="analyze / manual / …"
              className={inputCls}
            />
          </div>
        </div>

        {/* Setup（交易原型） */}
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

        {/* 备注 */}
        <div>
          <label className={labelCls}>备注</label>
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className={inputCls}
          />
        </div>

        {mut.isError && (
          <p className="text-xs text-down">失败 · {String(mut.error)}</p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={mut.isPending}
          >
            取消
          </Button>
          <Button type="submit" variant="primary" disabled={mut.isPending}>
            {mut.isPending ? "保存中…" : "保存"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
