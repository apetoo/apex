/**
 * verdict → 颜色分组映射
 *
 * 同步自 apex/schemas.VERDICT_ENUM(后端 source of truth)。
 * 若后端新增 verdict 枚举, 需同步更新此表。
 *
 * 红涨绿跌铁律: 看多系 → up(红), 中性系 → flat(灰), 看空系 → down(绿)
 *
 * (ED7: 原方案是后端加 /api/meta/verdicts 端点, outside voice #3 判定过度工程,
 *  撤销, 改用 TS const。颜色是前端展示关注点, VERDICT_ENUM 极少变。)
 */
export type VerdictColor = "up" | "flat" | "down";

export const VERDICT_COLOR: Record<string, VerdictColor> = {
  // 看多系 → 红(涨)
  看多: "up",
  偏多: "up",
  观望偏多: "up",
  // 中性系 → 灰
  中性: "flat",
  观望: "flat",
  // 看空系 → 绿(跌)
  观望偏空: "down",
  偏空: "down",
  看空: "down",
};

export function verdictColor(verdict: string | null | undefined): VerdictColor {
  if (!verdict) return "flat";
  return VERDICT_COLOR[verdict] ?? "flat";
}
