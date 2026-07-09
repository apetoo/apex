/**
 * 「我的交易系统」共享常量与类型（ADR-0001 / ADR-0002）
 *
 * 与 apex/schemas.py 的 SETUP_SEED / RULE_ITEMS 保持同源。
 * setup = 用户声明的交易原型维度（与 strategy 决策来源正交）；
 * rule_checklist = 用户在 promote 时提交的结构化 Rule 检查表 + 备注。
 */

/** AI 预填 / 用户确认的交易原型种子词表（可扩展，用户可加自定义） */
export const SETUP_SEED = [
  "打板",
  "首板",
  "龙回头",
  "板块轮动",
  "超跌反弹",
  "趋势突破",
  "业绩驱动",
  "题材炒作",
  "低位反转",
] as const;

/** Rule 检查表项 key 词表（结构化、可自动评分） */
export const RULE_ITEMS = [
  "entry_band",
  "stop_formula",
  "sizing_cap",
  "no_average_down",
  "max_hold_days",
  "sector_conc_cap",
  "no_chase",
] as const;

export const RULE_ITEM_LABELS: Record<string, string> = {
  entry_band: "入场带（±容差）",
  stop_formula: "止损按规则设置",
  sizing_cap: "仓位上限",
  no_average_down: "不加仓于亏损",
  max_hold_days: "最大持仓天数",
  sector_conc_cap: "板块集中度上限",
  no_chase: "不追高",
};

export interface RuleChecklistItem {
  key: string;
  params?: Record<string, number | string>;
  /** True=遵守 / False=违规 / null=未覆盖（入场时提交一律 null，close 后由守规引擎评估） */
  checked?: boolean | null;
}

export interface RuleChecklist {
  items: RuleChecklistItem[];
  note?: string;
}
