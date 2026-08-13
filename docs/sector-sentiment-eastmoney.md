# 东方财富股吧情绪采集

该采集器仅用于本地个人研究，使用 Scrapy 低频访问公开免登录页面。它不登录、不处理验证码、不轮换代理，也不会把 Cookie、作者昵称或本机路径提供给 API。

## 配置与运行

1. 复制 `config.example.yaml` 中的 `sector_sentiment.eastmoney` 配置到本地 `config.yaml`。
2. 保持 `platforms` 为短视频评分平台，把 `eastmoney.enabled` 设为 `true`、`phase` 保持 `shadow`，并为每个词典项人工填写已核验的 `eastmoney_forum_id`。
3. 将 `author_salt` 换成本机随机值。该值及任何登录信息均不得提交。
4. 安装锁定依赖后运行：

   ```bash
   python -c "from apex.sector_sentiment import run_configured; import pprint; pprint.pp(run_configured())"
   ```

页面的东方财富卡片区分当前采集状态与展示状态。若当前采集失败但存在上一成功批次，当前失败仍保留审计，页面明确显示“展示上一成功批次”，并把整体质量标为数据不足/降级，而不是低风险。

东方财富按自身采集记录累计14个“质量达标日”，失败或仅尝试的日期不计入达标进度。`shadow` 阶段仍入库和展示遥测，但证据不进入评分、覆盖门槛或状态机，采集参数也不进入活跃评分策略哈希。

14个质量达标日只是东方财富数据源的最低技术验收条件，不能覆盖或提前结束正在进行的60交易日冻结 cohort。若当前 cohort 尚未完成，必须继续保持 `shadow`；只有冻结期完成且人工复核通过后才切换 `phase: promoted`。该切换会产生新的评分策略哈希，并从新的60日验证 cohort 开始，东方财富才进入评分并成为财经社区覆盖门槛。
