# 0002 - Sample-gated disclosure: no metric recommends below its confidence threshold

The "my trading system" page operates on a closed-trade sample that is currently n=4 and will grow slowly. Every metric on the page carries its n and a confidence band. Below fixed thresholds (slice attribution n<20; rule/setup expectancy n<30) a metric is still *shown* but explicitly flagged "低样本，仅描述不推断" and is never used to drive a recommendation or a "your edge is X" claim. Tier-1 process metrics (behavior descriptors, AI-Adherence, Discipline Score) are honest at any n and always shown; Tier-2 outcome metrics (Performance Attribution, Rule Discovery) render in an "accumulating, not yet conclusive" state until their threshold.

Rejected: hiding low-n metrics entirely (the page would feel empty and the user could not see accumulation progress - demotivating); showing everything without caveat (n=4 would let偶然盈亏 be packaged as "your edge" - the classic retail-journal self-deception trap the user explicitly wants to avoid).

This is a deliberate deviation from "give the user the answers they asked for": the page refuses to pronounce on profitability until the data earns it. Future maintainers should not "fix" this by lowering or removing the thresholds - the thresholds are the point.
