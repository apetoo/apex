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
 * 安全转数字: 后端/表单常常把数字序列化成字符串(null/"12.34"/"NaN"),
 * 直接 .toFixed 会抛 "X.toFixed is not a function"。这里统一兜底成 number | null。
 * 非有限数(Infinity/NaN/非法字符串) → null, 调用方据此显示 "—"。
 */
function safeNum(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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
export function formatPrice(price: unknown): string {
  // 0 视为无值: 后端看多类价位被拒后理论上不会再漏 0 进来,
  // 但历史 journal + 非看多方向占位仍可能拿到 0, A 股没有 0 元的票
  const n = safeNum(price);
  if (n == null || n === 0) return "—";
  return n.toFixed(2);
}

/**
 * 格式化涨跌幅: 参数已经是百分比值(8.2 表示 8.2%), 带正负号
 * 用于: net_return (0.082 → "+8.20%"), 涨跌幅已乘 100 的场景
 */
export function formatPercent(pct: unknown): string {
  const n = safeNum(pct);
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

/**
 * 格式化比率(0-1 之间的小数): 内部 × 100 显示为百分比, 无正负号
 * 用于: 胜率 (0.6 → "60%"), 通过率, 分数, 权重归一化
 */
export function formatRatio(ratio: unknown, digits = 0): string {
  const n = safeNum(ratio);
  if (n == null) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

/**
 * 格式化涨跌额: 带正负号, 2 位小数
 */
export function formatDelta(delta: unknown): string {
  const n = safeNum(delta);
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
}

/**
 * 格式化成交金额(元): 千分位 + "元"。null/0/NaN → —。
 * 用于交易流水 amount = fill_price × shares(买入=花出, 卖出=回笼)。
 */
export function formatAmount(amount: number | null | undefined): string {
  if (amount == null || amount === 0 || Number.isNaN(amount)) return "—";
  return `${amount.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 元`;
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

/**
 * ts_code 归一化(镜像后端 data.normalize_ts_code:
 * 6/5->SH, 0/3/1->SZ, 4/8/9->BJ)。
 * 接受 "601318" / "601318.SH" / "601318.sh"，非法返回 null。
 */
export function normalizeTsCode(raw: string): string | null {
  const s = raw.trim().toUpperCase();
  if (!/^\d{6}(\.(SH|SZ|BJ))?$/.test(s)) return null;
  if (s.length === 6) {
    const d = s[0];
    const suf =
      d === "6" || d === "5"
        ? "SH" // 6 沪市个股, 5 沪市 ETF/基金
        : d === "0" || d === "3" || d === "1"
          ? "SZ" // 0/3 深市个股, 1 深市 ETF/基金
          : d === "4" || d === "8" || d === "9"
            ? "BJ"
            : null;
    return suf ? `${s}.${suf}` : null;
  }
  return s;
}
