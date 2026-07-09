# Apex Trading System

The personal A-share trading-loop domain: AI analysis, watchlist/journal, and the emerging "my trading system" layer that turns trading history into a self-authored, adherence-tracked rule system. This file is a glossary only — no implementation details.

## Language

### Decision inputs (existing)

**Verdict**:
The AI analyst's directional call on a stock, on the enum 看多/偏多/观望偏多/中性/观望偏空/偏空/看空.
_Avoid_: rating, signal

**Confidence**:
The AI's self-reported strength of the verdict (1–10), before any calibration.
_Avoid_: score

**Calibrated Confidence**:
Confidence adjusted by the historical hit-rate of the same Verdict@Confidence bucket.
_Avoid_: adjusted score

**Regime**:
The market-environment label in force when a trade is opened (breadth/trend state). Conditions every edge — the same Setup pays differently under different Regimes.
_Avoid_: market, trend

**Strategy** (decision source):
How a position's decision originated — `manual` (user-decided) or `analyze` (AI-driven). A *source* tag, not a trade archetype.
_Avoid_: setup, playbook, type

### The "my trading system" layer (new)

**AI Plan**:
The entry / stop-loss / target / sizing the AI journal prescribes at analysis time. One of two things Adherence can be measured against.
_Avoid_: rule (it is the AI's prescription, not the user's)

**Setup**:
A user-declared trade archetype tagged at entry — e.g. 打板, 板块轮动, 超跌反弹. The clustering key for later edge-mining. Orthogonal to Strategy.
_Avoid_: 策略, strategy, type

**Rule**:
A user-authored trading rule (entry condition / position sizing / exit discipline) the user commits to follow. The second thing Adherence can be measured against.
_Avoid_: strategy, signal, plan

**Adherence (守规)**:
Whether a trade's actual actions matched the declared plan — the AI Plan (AI守规层) or the user's own Rule (自录层). A discipline measure, independent of outcome.
_Avoid_: 命中率, win rate (those are outcome, not discipline)

### Behavior engine (new)

**Behavior Proxy**:
A formula-defined observable stand-in for a behavior that cannot be measured directly - e.g. revenge-trade timing (a new position opened within N days of a realized loss) standing in for "情绪化". Every behavioral label on the page resolves to one or more Behavior Proxies, never to a self-tag or an AI guess.
_Avoid_: 情绪化 (as a primitive - it is always a proxy), sentiment

**Discipline Score**:
A per-trade and aggregate 0-1 quantification of Adherence - the fraction of committed Rule checklist items (and AI-Plan conditions) the trade's actual actions satisfied. A process measure, shown at any n; never used to claim edge.
_Avoid_: 命中率, accuracy
