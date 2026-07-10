/**
 * 板块预设 - 板块名 -> 代表性板块 ETF 代码(6 位无后缀, normalizeTsCode 补后缀)。
 * 纯前端常量: 改完刷新即生效。用板块 ETF 看板块整体分时走势(一条线),
 * 想看个股细节可在输入框手输个股/ETF 代码。
 *
 * ETF 选取(新浪分时全支持, 实测 stock_zh_a_minute rows=1970):
 *   CPO/通信 515880 / 创新药 159992 / 存储·芯片 159995 / 机器人 562500
 *   半导体 512480 / 保险 512070 / 证券 512880
 */
export const SECTOR_PRESETS: Record<string, string[]> = {
  CPO: ["515880"],
  创新药: ["159992"],
  存储芯片: ["159995"],
  机器人: ["562500"],
  半导体: ["512480"],
  保险: ["512070"],
  证券: ["512880"],
};

/** 板块按钮顺序(Record 键序不保证, 显式列出) */
export const SECTOR_ORDER: string[] = [
  "CPO",
  "创新药",
  "存储芯片",
  "机器人",
  "半导体",
  "保险",
  "证券",
];

/**
 * 板块 ETF 名称预设。ETF 不在 tushare stock_basic, getStockInfo 拿不到名称,
 * 这里预设让卡片头显示中文名; 手输的其他 ETF/个股仍走 getStockInfo。
 */
export const ETF_NAMES: Record<string, string> = {
  "515880.SH": "通信ETF",
  "159992.SZ": "创新药ETF",
  "159995.SZ": "芯片ETF",
  "562500.SH": "机器人ETF",
  "512480.SH": "半导体ETF",
  "512070.SH": "保险ETF",
  "512880.SH": "证券ETF",
};
