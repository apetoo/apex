---
name: monthly-review
description: apex 月度交易复盘。调用后端跑回测/平仓复盘/AI校准/纪律分析，汇总成月报并落盘。每月初 cron 触发，或手动 /monthly-review，或用户说"跑月度复盘/月报"。只读+提案，不改 config、不动持仓、不下单。
---

# apex 月度复盘

## 何时用
- 每月初 cron 自动触发
- 手动 `/monthly-review`
- 用户说「跑一下月度复盘 / 出月报 / 看看这个月交易表现」

## 步骤

### 1. 采集数据（用现成脚本，别自己拼 curl）
```bash
cd <apex 仓库目录>
APEX_API=http://127.0.0.1:8000/api MONTH_DAYS=30 \
  python hermes/skills/monthly-review/gather.py > /tmp/apex_monthly.json
```
- 先 `curl -s http://127.0.0.1:8000/api/health` 确认后端在跑；不在跑就停下告诉用户去启 `uvicorn`。
- 想要 AI 复盘整批信号（贵，会调 DeepSeek）：加 `WITH_AI_REVIEW=1`。
- 读 `/tmp/apex_monthly.json` 里的 `_saved` 字段，原始 JSON 也落在了 `reports/monthly-YYYY-MM.json`。

### 2. 按 JSON 字段写月报到 `reports/monthly-YYYY-MM.md`

**1) 本月实盘成绩** — `realized` + `trades`
- 实际平仓 PnL、胜率、平均持有期
- 佣金 + 印花税占毛利比例
- 买卖笔数、净现金流

**2) 信号回测（模拟）** — `portfolio` + `signals`
- 组合净值、总收益、夏普、最大回撤
- 信号胜率 / 平均收益
- ⚠ 信号数 `n < 30` 必须写「样本不足，不下结论」（ADR-0002）

**3) AI 校准** — `calibration`
- verdict × confidence 桶胜率
- `calibrated_confidence` vs 原始 confidence：AI 高估还是低估自己？

**4) 交易纪律** — `system`（trading_dna / behavior / discipline）
- 止损遵守率、目标遵守率
- 补仓（加仓）行为、持有期分布
- **逐条点名纪律漏点**：哪笔没守止损 / 没到目标就跑 / 冲动加仓

**5) 平仓复盘** — `postmortems_run`
- 每笔平仓 `diagnosis`：exit_reason 是否合理、学到什么
- `ok:false` 的（复盘失败）列出来

**6) AI 复盘（若有）** — `ai_review`
- `findings` / `strategy_weight_hint`

**7) 下月建议（仅提案，写进 `proposals/YYYY-MM.md`）**
- 每条建议给出动机（引用上面哪条数据）+ 具体改动（config diff 或操作）
- 不要在本月报里直接改任何东西

### 3. 红线（每次都要守）
- ❌ 不改 `config.yaml`、不调策略权重/因子开关
- ❌ 不动持仓：不 `promote`/`buy`/`sell`/`close`/`archive`
- ❌ 不下单
- ✅ 样本 `n < 30` 的结论标注「不下结论」
- ✅ 只输出报告 + `proposals/`，状态变更全部走人审

### 4. 收尾
- 把 `reports/monthly-YYYY-MM.md` 路径回告用户
- cron 触发时，再给一句话 Telegram 摘要：**本月实盘 PnL + 最大的 1 个纪律漏点 + 1 条提案**
