"""选股策略 —— 头等公民。每个策略以 module 形式存在，暴露：

  NAME         : str             唯一标识；写到持仓 record 的 strategy 字段
  DESCRIPTION  : str             适用场景描述；S2 起 AI 选策略时读这段文本
  select(by_source: dict, regime: dict | None) -> list[dict]
                                  从原始信号产出该策略的候选；不需要打分
  score_one(candidate: dict, regime: dict | None) -> float
                                  规则评分 0-1；快速、无需 LLM

candidate dict 协议:
  {
    "ts_code": "...", "name": "...",
    "signals": [SignalRecord, ...],     # 选中此股的依据（喂 AI 用）
    "strategy_reasoning": "<≤80 字>",    # 自然语言描述，喂 AI 用
    "strategy_features": {...},         # 数值特征（score_one 内部用）
  }

screener.run() 在 select() 之后填上：
  candidate["strategy"] = NAME
  candidate["strategy_score"] = score_one(...)
"""
from apex.strategies import (
    first_board_leader,
    institutional_flow,
    industry_rotation,
    leader_with_volume,
    pullback_to_ma,
    bullish_alignment,
    volume_breakout,
)

STRATEGIES = {
    s.NAME: s for s in [
        first_board_leader,
        institutional_flow,
        industry_rotation,
        leader_with_volume,
        pullback_to_ma,
        bullish_alignment,
        volume_breakout,
    ]
}
