import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * cn — 合并 className, tailwind-merge 去重冲突
 * 用法: cn("px-2", condition && "px-4") → "px-4"
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * A 股方向 → 颜色 class
 * 红涨绿跌(铁律): >0 → up, <0 → down, 0/null → flat
 */
export function directionClass(delta: number | null | undefined): string {
  if (delta == null || Number.isNaN(delta) || delta === 0) return "text-flat";
  return delta > 0 ? "text-up" : "text-down";
}

/**
 * 格式化价格: 保留 2 位小数, 等宽
 */
export function formatPrice(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return "—";
  return price.toFixed(2);
}

/**
 * 格式化涨跌幅: 参数已经是百分比值(8.2 表示 8.2%), 带正负号
 * 用于: net_return (0.082 → "+8.20%"), 涨跌幅已乘 100 的场景
 */
export function formatPercent(pct: number | null | undefined): string {
  if (pct == null || Number.isNaN(pct)) return "—";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

/**
 * 格式化比率(0-1 之间的小数): 内部 × 100 显示为百分比, 无正负号
 * 用于: 胜率 (0.6 → "60%"), 通过率, 分数, 权重归一化
 */
export function formatRatio(ratio: number | null | undefined, digits = 0): string {
  if (ratio == null || Number.isNaN(ratio)) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}

/**
 * 格式化涨跌额: 带正负号, 2 位小数
 */
export function formatDelta(delta: number | null | undefined): string {
  if (delta == null || Number.isNaN(delta)) return "—";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${delta.toFixed(2)}`;
}

/**
 * 成交额 千元 → 亿元(÷1e5)。
 * tushare index_daily 的 amount 单位是千元, 新浪实时 amount 已在后端 ÷1000 对齐到千元。
 * 1 亿 = 1e5 千元, 故 ÷1e5。null/undefined/NaN → null(便于上层判空)。
 */
export function amountKToYi(amtK: number | null | undefined): number | null {
  if (amtK == null || Number.isNaN(amtK)) return null;
  return amtK / 1e5;
}

/**
 * 亿元 → 显示串: ≥1e4 亿显示「X.XX 万亿」, 否则「XXXX 亿」(整数)。
 * 用于两市合计成交额。null/undefined/NaN → —。
 */
export function formatVolume(yi: number | null | undefined): string {
  if (yi == null || Number.isNaN(yi)) return "—";
  if (yi >= 10000) return `${(yi / 10000).toFixed(2)} 万亿`;
  return `${yi.toFixed(0)} 亿`;
}
