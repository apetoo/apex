# 手动持仓 + 买卖留痕 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在持仓页手动开仓(代码+成本+股数)、对已有持仓加仓/减仓,每笔买卖写入 `trades.jsonl` 留痕,卖光自动走平仓复盘。

**Architecture:** 持仓为主 + 旁路交易流水。`active_positions` 随买卖变动(新增 `avg_cost` 字段),`trades.jsonl` 每笔买卖追加一行留痕,`closed_positions.jsonl` 形状不变。新增 `apex/trades.py` 模块隔离流水审计;`buy`/`sell` 加在 `apex/watchlist.py`(与 `close_position` 同族,sell 卖光路径直接复用它)。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic(后端);Vite + React 19 + TypeScript + TanStack Query v5(前端)。

## Global Constraints

- **无测试套件**(CLAUDE.md 约定):不用 pytest。每个任务用 `python -c "import ast; ast.parse(open('<file>').read())"` 验语法 + tempdir smoke-test 脚本验行为。smoke-test 时 `postmortem=False` 避免真实 DeepSeek 调用。
- **ts_code 规范化**:所有后端入口经 `data.normalize_ts_code()`(6→SH, 0/3→SZ, 4/8→BJ)。
- **A 股红涨绿跌**:前端 `text-up`(红)/`text-down`(绿)。
- **SSE 不涉及**(本轮无流式端点)。
- **异常映射复用** `backend/core/errors.py`:ValueError→400,PositionNotFoundError→404。路由无 try/except。
- **存储路径**:`~/.stock-journal/`(journal_dir),trades.jsonl 与 closed_positions.jsonl 同目录。
- **频繁提交**:每个任务结束 commit。

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `apex/trades.py` | 交易流水审计:append_trade / load_trades | 新建 |
| `apex/watchlist.py` | 持仓状态机:add_position 加 avg_cost;新增 buy/sell;close_position pnl 基准;迁移补 avg_cost | 修改 |
| `backend/schemas/watchlist.py` | 请求模型:新增 Buy/SellRequest;AddPosition 止损目标改可选 | 修改 |
| `backend/routers/watchlist.py` | 路由:新增 /buy /sell /trades | 修改 |
| `frontend/src/api/watchlist.ts` | API 客户端:buy/sell/getTrades;avg_cost;Trade 类型 | 修改 |
| `frontend/src/api/mutations.ts` | useBuy/useSell + 失效矩阵 | 修改 |
| `frontend/src/api/query-keys.ts` | trades key 工厂 | 修改 |
| `frontend/src/routes/watchlist/WatchlistPage.tsx` | 开仓表单 + 加仓/减仓 + 流水 Card | 修改 |
| `frontend/src/components/a-share/PositionCard.tsx` | PnL 用 avg_cost + 展示均价/股数 + 加仓/减仓按钮 | 修改 |

---

### Task 1: `apex/trades.py` — 交易流水审计模块

**Files:**
- Create: `apex/trades.py`

**Interfaces:**
- Produces: `append_trade(**fields) -> dict`(组装并追加一行到 trades.jsonl,返回该 dict);`load_trades(ts_code=None, limit=None, since_days=None) -> list[dict]`(按 traded_at 倒序,可过滤);`_next_trade_id(now) -> str`。

- [ ] **Step 1: 创建 `apex/trades.py`**

```python
"""交易流水审计:每笔买入/卖出追加一行到 ~/.stock-journal/trades.jsonl。

留痕供未来 AI 操作诊断(买卖时机/加仓减仓节奏)。本轮只写不分析。
字段形状见 docs/superpowers/specs/2026-06-27-manual-positions-trades-design.md。
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_TZ_CN = timezone(timedelta(hours=8))


def _path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "trades.jsonl"


def _next_trade_id(now: datetime) -> str:
    """YYYYMMDDTHHMMSS-<4位序号>。同秒多笔按文件内已有同秒计数 +1。"""
    stamp = now.strftime("%Y%m%dT%H%M%S")
    seq = 0
    p = _path()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("trade_id") or "").startswith(stamp):
                    seq += 1
    return f"{stamp}-{seq:04d}"


def append_trade(*,
                 ts_code: str,
                 name: str,
                 side: str,
                 fill_price: float,
                 shares: int,
                 avg_cost_after: Optional[float],
                 shares_after: Optional[int],
                 strategy: Optional[str] = None,
                 regime: Optional[str] = None,
                 journal_ref: Optional[dict] = None,
                 realized_pnl: Optional[float] = None,
                 realized_pnl_pct: Optional[float] = None,
                 note: str = "") -> dict:
    """组装一条 trade 记录并追加到 trades.jsonl。返回写入的 dict。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须 buy|sell, got {side}")
    now = datetime.now(_TZ_CN)
    record = {
        "trade_id": _next_trade_id(now),
        "ts_code": ts_code,
        "name": name,
        "side": side,
        "fill_price": round(float(fill_price), 4),
        "shares": int(shares),
        "amount": round(float(fill_price) * int(shares), 2),
        "realized_pnl": round(float(realized_pnl), 2) if realized_pnl is not None else None,
        "realized_pnl_pct": round(float(realized_pnl_pct), 4) if realized_pnl_pct is not None else None,
        "avg_cost_after": round(float(avg_cost_after), 4) if avg_cost_after is not None else None,
        "shares_after": int(shares_after) if shares_after is not None else None,
        "strategy": strategy,
        "regime": regime,
        "journal_ref": journal_ref,
        "note": note or "",
        "traded_at": now.isoformat(timespec="seconds"),
    }
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def load_trades(ts_code: Optional[str] = None,
                limit: Optional[int] = None,
                since_days: Optional[int] = None) -> list[dict]:
    """读 trades.jsonl,按 traded_at 倒序。可按 ts_code / limit / since_days 过滤。"""
    p = _path()
    if not p.exists():
        return []
    cutoff: Optional[str] = None
    if since_days:
        cutoff = (datetime.now(_TZ_CN) - timedelta(days=since_days)).isoformat()
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ts_code and rec.get("ts_code") != ts_code:
                continue
            if cutoff and (rec.get("traded_at") or "") < cutoff:
                continue
            out.append(rec)
    out.sort(key=lambda r: r.get("traded_at") or "", reverse=True)
    if limit:
        out = out[:limit]
    return out
```

- [ ] **Step 2: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/trades.py').read())"`
Expected: 无输出(语法 OK)。

- [ ] **Step 3: 写 tempdir smoke-test 脚本**

创建 `/tmp/smoke_trades.py`:

```python
import os, tempfile, sys
os.environ.setdefault("TUSHARE_TOKEN", "x")  # config.load 可能不依赖, 但保险
# 临时改 config 路径
sys.path.insert(0, ".")
from apex import config, trades as t

with tempfile.TemporaryDirectory() as d:
    # 注入临时 journal_dir
    cfg = config.get()
    cfg["paths"]["journal_dir"] = d
    # config 是单例, 直接改其返回的 dict
    r1 = t.append_trade(ts_code="002050.SZ", name="三花智控", side="buy",
                        fill_price=12.5, shares=1000, avg_cost_after=12.5,
                        shares_after=1000, strategy="manual", note="开仓")
    r2 = t.append_trade(ts_code="002050.SZ", name="三花智控", side="sell",
                        fill_price=13.0, shares=300, avg_cost_after=12.5,
                        shares_after=700, realized_pnl=150.0,
                        realized_pnl_pct=0.04, strategy="manual", note="减仓")
    assert r1["trade_id"] != r2["trade_id"], "trade_id 应唯一"
    assert r1["amount"] == 12500.0, r1["amount"]
    assert r2["realized_pnl"] == 150.0
    loaded = t.load_trades(ts_code="002050.SZ")
    assert len(loaded) == 2, loaded
    assert loaded[0]["side"] == "sell", "倒序: sell 在前"  # traded_at 同秒, 倒序看 id
    assert len(t.load_trades(limit=1)) == 1
    assert t.load_trades(ts_code="999999.SZ") == []
    print("OK trades smoke")
```

- [ ] **Step 4: 跑 smoke-test**

Run: `python /tmp/smoke_trades.py`
Expected: `OK trades smoke`

> 若 `config.get()` 报缺配置,在脚本顶部加 `os.environ["HOME"]` 指向 tempdir 或确认 `config.yaml` 可加载。config 是单例,改返回 dict 的 `paths.journal_dir` 即生效。

- [ ] **Step 5: Commit**

```bash
git add apex/trades.py
git commit -m "feat(apex): 新增 trades.py 交易流水审计模块

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `apex/watchlist.py` — `add_position` 加 `avg_cost`,止损/目标改可选

**Files:**
- Modify: `apex/watchlist.py`(`add_position` 函数,约 143-199 行)

**Interfaces:**
- Consumes: 无新依赖
- Produces: `add_position` 现在写入 `avg_cost = entry_price`;`stop_loss`/`target` 仍接收但调用方可传 None(为 Task 4 buy 开仓铺路,本任务只动 add_position 本身)

- [ ] **Step 1: 修改 `add_position` 写入 avg_cost**

在 `apex/watchlist.py` 的 `add_position` 函数里,`record` dict 构造处(约 166-177 行),在 `"entry_price": entry_price,` 后加一行,并把 stop_loss/target 保持写入(它们仍是参数,但 Task 3 会把签名改 Optional;本步先加 avg_cost 字段):

把:
```python
    record: dict = {
        "ts_code": ts_code,
        "name": name,
        "entry_price": entry_price,
        "entry_date": date.today().isoformat(),
        "stop_loss": stop_loss,
        "target": target,
        "trigger_price": trigger_price,
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "status": "active",
    }
```
改成:
```python
    record: dict = {
        "ts_code": ts_code,
        "name": name,
        "entry_price": entry_price,
        "avg_cost": float(entry_price),
        "entry_date": date.today().isoformat(),
        "stop_loss": stop_loss,
        "target": target,
        "trigger_price": trigger_price,
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "status": "active",
    }
```

- [ ] **Step 2: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 3: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): add_position 写入 avg_cost=entry_price

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `apex/watchlist.py` — `add_position` 止损/目标改 Optional

**Files:**
- Modify: `apex/watchlist.py`(`add_position` 签名 + record 构造,以及 `replace_position`/`promote_candidate` 透传)

**Interfaces:**
- Produces: `add_position` 的 `stop_loss`/`target` 参数类型改 `Optional[float]`,缺则不写 key(允许手动票不填止损/目标)。

- [ ] **Step 1: 改 `add_position` 签名**

把 `add_position` 签名(约 143-152 行)中的:
```python
def add_position(ts_code: str, name: str, entry_price: float,
                 stop_loss: float, target: float,
```
改成:
```python
def add_position(ts_code: str, name: str, entry_price: float,
                 stop_loss: Optional[float], target: Optional[float],
```

- [ ] **Step 2: record 里 stop_loss/target 改条件写入**

把上一步刚加完 avg_cost 的 record 块中的:
```python
        "stop_loss": stop_loss,
        "target": target,
```
删掉这两行,然后在 record 构造之后、`if position_size_shares is not None` 之前,插入条件写入。即把:
```python
    if position_size_shares is not None and position_size_shares > 0:
        record["position_size_shares"] = int(position_size_shares)
```
改成:
```python
    if stop_loss is not None:
        record["stop_loss"] = float(stop_loss)
    if target is not None:
        record["target"] = float(target)
    if position_size_shares is not None and position_size_shares > 0:
        record["position_size_shares"] = int(position_size_shares)
```

- [ ] **Step 3: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): add_position 止损/目标改可选, 缺则不写 key

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `apex/watchlist.py` — 新增 `buy()` 开仓/加仓

**Files:**
- Modify: `apex/watchlist.py`(在 `promote_candidate` 之后、`dedup_active_positions` 之前插入新函数)

**Interfaces:**
- Consumes: `apex.trades.append_trade`、`apex.data.normalize_ts_code`(调用方做)、`apex.data.get_stock_info`(反查名称)、`apex.regime.load`、`apex.journal.load_entries`
- Produces: `buy(ts_code, fill_price, shares, stop_loss=None, target=None, note="", strategy="manual", regime=None) -> dict` 返回 `{"position": <record>, "trade": <trade>}`

- [ ] **Step 1: 在文件顶部确保 imports**

`watchlist.py` 已 import `json`/`date`/`datetime`/`Optional`。新增 buy 需要 `datetime.now(_TZ_CN)`(已用)和按需 lazy import data/regime/journal(对齐现有 `close_position` 的 lazy import 风格,避免循环)。无需改顶部 import。

- [ ] **Step 2: 加两个辅助函数 + `buy`**

在 `promote_candidate` 函数之后(约 253 行后)插入:

```python
def _today_regime() -> Optional[str]:
    """今日 regime label(无缓存返回 None, 不主动 collect 避免延迟)。"""
    try:
        from apex import regime as _regime_mod
        today_iso = date.today().isoformat()
        r = _regime_mod.load(today_iso)
        if r and r.get("label"):
            return r["label"]
    except Exception:
        pass
    return None


def _journal_ref_for(ts_code: str) -> Optional[dict]:
    """下单时该股最近一条 journal entry(无则 None)。供 AI 诊断关联开仓上下文。"""
    try:
        from apex import journal as _journal
        entries = _journal.load_entries(ts_code=ts_code)
        if not entries:
            return None
        latest = sorted(entries, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]
        return {
            "verdict": latest.get("verdict"),
            "confidence": latest.get("confidence"),
            "analyzed_at": latest.get("analyzed_at") or latest.get("date"),
        }
    except Exception:
        return None


def buy(ts_code: str, fill_price: float, shares: int,
        stop_loss: Optional[float] = None, target: Optional[float] = None,
        note: str = "", strategy: str = "manual",
        regime: Optional[str] = None) -> dict:
    """买入:持仓存在则加仓(重算 avg_cost),不存在则开仓。同时追加一条 buy trade 留痕。

    返回 {"position": <更新后 record>, "trade": <写入的 trade>}。

    Raises:
      ValueError: fill_price<=0 或 shares<=0
    """
    from apex import data as _data
    from apex import trades as _trades

    if fill_price is None or float(fill_price) <= 0:
        raise ValueError(f"fill_price 必须 > 0, got {fill_price}")
    if shares is None or int(shares) <= 0:
        raise ValueError(f"shares 必须 > 0, got {shares}")

    fill_price = float(fill_price)
    shares = int(shares)
    wl = _load()
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)

    regime_label = regime if regime is not None else _today_regime()
    journal_ref = _journal_ref_for(ts_code)

    if existing is not None:
        # 加仓
        old_shares = int(existing.get("position_size_shares") or 0)
        old_avg = float(existing.get("avg_cost") or existing.get("entry_price") or 0)
        new_shares = old_shares + shares
        if new_shares > 0:
            new_avg = (old_avg * old_shares + fill_price * shares) / new_shares
        else:
            new_avg = fill_price
        existing["position_size_shares"] = new_shares
        existing["avg_cost"] = round(new_avg, 4)
        if stop_loss is not None:
            existing["stop_loss"] = float(stop_loss)
        if target is not None:
            existing["target"] = float(target)
        position = existing
    else:
        # 开仓: 反查名称
        name = ""
        try:
            info_raw = _data.get_stock_info(ts_code=ts_code)
            info_list = json.loads(info_raw) if isinstance(info_raw, str) else info_raw
            if isinstance(info_list, list) and info_list:
                name = info_list[0].get("name", "") or ""
        except Exception:
            pass
        record = {
            "ts_code": ts_code,
            "name": name,
            "entry_price": fill_price,
            "avg_cost": fill_price,
            "entry_date": date.today().isoformat(),
            "trigger_price": None,
            "trigger_direction": "below",
            "expires_at": (date.today() + timedelta(days=10)).isoformat(),
            "status": "active",
        }
        if stop_loss is not None:
            record["stop_loss"] = float(stop_loss)
        if target is not None:
            record["target"] = float(target)
        record["position_size_shares"] = shares
        if strategy:
            record["strategy"] = str(strategy)
        wl["active_positions"].append(record)
        position = record
        new_shares = shares
        new_avg = fill_price

    _save(wl)
    trade = _trades.append_trade(
        ts_code=ts_code, name=position.get("name", ""),
        side="buy", fill_price=fill_price, shares=shares,
        avg_cost_after=position.get("avg_cost"), shares_after=position.get("position_size_shares"),
        strategy=position.get("strategy") or strategy, regime=regime_label,
        journal_ref=journal_ref, realized_pnl=None, note=note,
    )
    return {"position": position, "trade": trade}
```

- [ ] **Step 3: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 4: 写 smoke-test 脚本**

创建 `/tmp/smoke_buy.py`:

```python
import os, sys, tempfile, json
sys.path.insert(0, ".")
from apex import config, watchlist as wl, trades as t

with tempfile.TemporaryDirectory() as d:
    cfg = config.get()
    cfg["paths"]["watchlist_file"] = os.path.join(d, "watchlist.json")
    cfg["paths"]["journal_dir"] = d

    # 开仓 (反查名称会失败因没 tushare, name 空串 OK)
    r1 = wl.buy("002050.SZ", fill_price=12.0, shares=1000, strategy="manual", note="开仓")
    assert r1["position"]["avg_cost"] == 12.0
    assert r1["position"]["position_size_shares"] == 1000
    assert r1["trade"]["side"] == "buy"
    assert r1["trade"]["shares_after"] == 1000

    # 加仓: avg_cost 应为 (12*1000 + 13*500)/1500 = 12.333...
    r2 = wl.buy("002050.SZ", fill_price=13.0, shares=500, note="加仓")
    assert r2["position"]["position_size_shares"] == 1500
    assert abs(r2["position"]["avg_cost"] - round((12*1000+13*500)/1500, 4)) < 1e-6, r2["position"]["avg_cost"]
    assert r2["trade"]["shares_after"] == 1500

    # 流水应有 2 条 buy
    trs = t.load_trades(ts_code="002050.SZ")
    assert len(trs) == 2 and all(x["side"] == "buy" for x in trs), trs

    # 持仓仍是一条
    data = wl.load()
    assert len(data["active_positions"]) == 1
    print("OK buy smoke")
```

- [ ] **Step 5: 跑 smoke-test**

Run: `python /tmp/smoke_buy.py`
Expected: `OK buy smoke`

- [ ] **Step 6: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): 新增 buy() 开仓/加仓 + 追加 buy trade 留痕

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: `apex/watchlist.py` — `close_position` pnl 基准改用 avg_cost

**Files:**
- Modify: `apex/watchlist.py`(`close_position` 函数,约 387-407 行)

**Interfaces:**
- Produces: `close_position` 优先用 `actual_fill_price`,否则 `avg_cost`,否则 `entry_price`。Task 6 的 sell 卖光路径会传 `actual_fill_price=avg_cost_before`。

- [ ] **Step 1: 改 fill_price 回落顺序**

在 `close_position` 中,把(约 388-389 行):
```python
    entry_price = float(pos.get("entry_price") or 0)
    fill_price = float(actual_fill_price) if actual_fill_price is not None else entry_price
```
改成:
```python
    entry_price = float(pos.get("entry_price") or 0)
    avg_cost = float(pos.get("avg_cost") or entry_price)
    fill_price = float(actual_fill_price) if actual_fill_price is not None else avg_cost
```

- [ ] **Step 2: open 段记录 avg_cost**

在 `close_position` 的 `record["open"]` 字典里(约 414-431 行),`"actual_fill_price": fill_price or None,` 之后加一行 `"avg_cost": avg_cost or None,`,即把:
```python
            "entry_price": entry_price or None,
            "actual_fill_price": fill_price or None,
```
改成:
```python
            "entry_price": entry_price or None,
            "avg_cost": avg_cost or None,
            "actual_fill_price": fill_price or None,
```

- [ ] **Step 3: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): close_position pnl 基准优先用 avg_cost, open 段记录 avg_cost

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: `apex/watchlist.py` — 新增 `sell()` 减仓/卖光

**Files:**
- Modify: `apex/watchlist.py`(`buy` 之后插入 `sell`)

**Interfaces:**
- Consumes: `apex.trades.append_trade`、`apex.postmortem.run_and_patch`、`apex.calibration.compute`、`close_position`(同模块)
- Produces: `sell(ts_code, fill_price, shares, exit_reason="manual", note="", postmortem=True) -> dict`。减仓返回 `{"position": <pos>, "trade": <sell trade>}`;卖光返回 `{"trade": <sell trade>, "closed_record": <rec>, "diagnosis": <diagnosis or None>}`。

- [ ] **Step 1: 在 `buy` 函数之后插入 `sell`**

```python
def sell(ts_code: str, fill_price: float, shares: int,
         exit_reason: str = "manual", note: str = "",
         postmortem: bool = True) -> dict:
    """卖出:减仓(改股数+实现 pnl,不复盘)或卖光(走 close_position + postmortem + calibration)。
    同时追加一条 sell trade 留痕。

    返回:
      减仓: {"position": <更新后 pos>, "trade": <sell trade>}
      卖光: {"trade": <sell trade>, "closed_record": <closed record>, "diagnosis": <diagnosis or None>}

    Raises:
      PositionNotFoundError: ts_code 不在 active_positions
      ValueError: fill_price<=0 / shares<=0 / shares > 持有
    """
    from apex import trades as _trades
    from apex.schemas import EXIT_REASON_ENUM

    if fill_price is None or float(fill_price) <= 0:
        raise ValueError(f"fill_price 必须 > 0, got {fill_price}")
    if shares is None or int(shares) <= 0:
        raise ValueError(f"shares 必须 > 0, got {shares}")
    if exit_reason not in EXIT_REASON_ENUM:
        exit_reason = "other"

    fill_price = float(fill_price)
    shares = int(shares)
    wl = _load()
    pos = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if pos is None:
        raise PositionNotFoundError(f"持仓 {ts_code} 不存在于 active_positions")

    holding = int(pos.get("position_size_shares") or 0)
    if shares > holding:
        raise ValueError(f"卖出股数 {shares} 超过持有 {holding}")

    avg_cost_before = float(pos.get("avg_cost") or pos.get("entry_price") or 0)
    regime_label = _today_regime()
    journal_ref = _journal_ref_for(ts_code)
    name = pos.get("name", "")
    strategy = pos.get("strategy")

    if shares >= holding:
        # 卖光 → 留痕 + 走 close_position(actual_fill_price=avg_cost_before 使 pnl 基准正确)
        realized = round((fill_price - avg_cost_before) * holding, 2) if avg_cost_before > 0 else None
        realized_pct = round((fill_price / avg_cost_before - 1), 4) if avg_cost_before > 0 else None
        trade = _trades.append_trade(
            ts_code=ts_code, name=name, side="sell",
            fill_price=fill_price, shares=holding,
            avg_cost_after=None, shares_after=0,
            strategy=strategy, regime=regime_label, journal_ref=journal_ref,
            realized_pnl=realized, realized_pnl_pct=realized_pct, note=note,
        )
        closed_record = close_position(
            ts_code=ts_code, exit_price=fill_price, exit_reason=exit_reason,
            actual_fill_price=avg_cost_before,
        )
        diagnosis = None
        if postmortem:
            try:
                from apex import postmortem as _pm
                diagnosis = _pm.run_and_patch(closed_record)
            except Exception:
                diagnosis = None
            try:
                from apex import calibration as _cal
                _cal.compute()
            except Exception:
                pass
        return {"trade": trade, "closed_record": closed_record, "diagnosis": diagnosis}

    # 减仓 → 留痕 + 改状态, 不复盘
    realized = round((fill_price - avg_cost_before) * shares, 2) if avg_cost_before > 0 else None
    realized_pct = round((fill_price / avg_cost_before - 1), 4) if avg_cost_before > 0 else None
    new_shares = holding - shares
    pos["position_size_shares"] = new_shares  # avg_cost 不变
    _save(wl)
    trade = _trades.append_trade(
        ts_code=ts_code, name=name, side="sell",
        fill_price=fill_price, shares=shares,
        avg_cost_after=avg_cost_before, shares_after=new_shares,
        strategy=strategy, regime=regime_label, journal_ref=journal_ref,
        realized_pnl=realized, realized_pnl_pct=realized_pct, note=note,
    )
    return {"position": pos, "trade": trade}
```

- [ ] **Step 2: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 3: 写 smoke-test 脚本(减仓 + 卖光,postmortem=False)**

创建 `/tmp/smoke_sell.py`:

```python
import os, sys, tempfile
sys.path.insert(0, ".")
from apex import config, watchlist as wl, trades as t

with tempfile.TemporaryDirectory() as d:
    cfg = config.get()
    cfg["paths"]["watchlist_file"] = os.path.join(d, "watchlist.json")
    cfg["paths"]["journal_dir"] = d

    wl.buy("002050.SZ", fill_price=10.0, shares=1000)  # avg_cost=10, 1000股

    # 减仓 300 @ 12 → realized (12-10)*300=600, 剩 700
    r1 = wl.sell("002050.SZ", fill_price=12.0, shares=300, postmortem=False)
    assert r1["position"]["position_size_shares"] == 700, r1["position"]
    assert r1["position"]["avg_cost"] == 10.0  # 减仓 avg_cost 不变
    assert r1["trade"]["realized_pnl"] == 600.0, r1["trade"]
    assert r1["trade"]["shares_after"] == 700

    # 卖光 700 @ 11 → realized (11-10)*700=700, 走 close_position(postmortem=False 不调 AI)
    r2 = wl.sell("002050.SZ", fill_price=11.0, shares=700, postmortem=False)
    assert "closed_record" in r2, r2
    assert r2["trade"]["realized_pnl"] == 700.0, r2["trade"]
    assert r2["trade"]["shares_after"] == 0
    data = wl.load()
    assert len(data["active_positions"]) == 0, "卖光后持仓应移除"
    assert len(data["archived"]) >= 1  # close_position 写了 breadcrumb

    # 卖超应报错
    wl.buy("002050.SZ", fill_price=10.0, shares=100)
    try:
        wl.sell("002050.SZ", fill_price=11.0, shares=200, postmortem=False)
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    print("OK sell smoke")
```

- [ ] **Step 4: 跑 smoke-test**

Run: `python /tmp/smoke_sell.py`
Expected: `OK sell smoke`

- [ ] **Step 5: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): 新增 sell() 减仓/卖光 + 留痕, 卖光复用 close_position

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: `apex/watchlist.py` — `migrate_and_backfill` 补 avg_cost

**Files:**
- Modify: `apex/watchlist.py`(`migrate_and_backfill` 函数,约 98-140 行)

**Interfaces:**
- Produces: 老持仓(无 avg_cost)迁移时补 `avg_cost = entry_price`。

- [ ] **Step 1: 在 migrate 循环里补 avg_cost**

在 `migrate_and_backfill` 的 `for section in (...)` 循环内,`if section in ("active_positions", "candidates") and not item.get("name")...` 块之后,加一段对 active_positions 补 avg_cost 的逻辑。即在该 `if section in ...` 块之后追加:

```python
            if section == "active_positions" and not item.get("avg_cost"):
                ep = item.get("entry_price")
                if ep is not None:
                    item["avg_cost"] = float(ep)
                    changed = True
```

- [ ] **Step 2: 语法校验**

Run: `python -c "import ast; ast.parse(open('apex/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 3: 写 smoke-test**

创建 `/tmp/smoke_migrate.py`:

```python
import os, sys, tempfile, json
sys.path.insert(0, ".")
from apex import config, watchlist as wl

with tempfile.TemporaryDirectory() as d:
    cfg = config.get()
    cfg["paths"]["watchlist_file"] = os.path.join(d, "watchlist.json")
    cfg["paths"]["journal_dir"] = d
    # 造一个老持仓(无 avg_cost)
    with open(os.path.join(d, "watchlist.json"), "w") as f:
        json.dump({"active_positions": [{
            "ts_code": "002050.SZ", "name": "三花", "entry_price": 12.0,
            "position_size_shares": 100, "entry_date": "2026-06-01", "status": "active"
        }], "candidates": [], "archived": []}, f)
    wl.migrate_and_backfill()
    data = wl.load()
    assert data["active_positions"][0].get("avg_cost") == 12.0, data["active_positions"][0]
    print("OK migrate smoke")
```

- [ ] **Step 4: 跑 smoke-test**

Run: `python /tmp/smoke_migrate.py`
Expected: `OK migrate smoke`

> migrate_and_backfill 内部会调 tushare 反查名称/normalize,可能因无 token 报错被 except 吞掉,不影响 avg_cost 补全断言。

- [ ] **Step 5: Commit**

```bash
git add apex/watchlist.py
git commit -m "feat(apex): migrate_and_backfill 补全老持仓 avg_cost=entry_price

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: `backend/schemas/watchlist.py` — 新增 Buy/SellRequest,AddPosition 止损目标改可选

**Files:**
- Modify: `backend/schemas/watchlist.py`

**Interfaces:**
- Produces: `BuyRequest`、`SellRequest`;`AddPositionRequest.stop_loss`/`target` 改 `Optional[float]`。

- [ ] **Step 1: 改 AddPositionRequest 止损/目标可选**

把 `AddPositionRequest` 中的:
```python
    entry_price: float
    stop_loss: float
    target: float
```
改成:
```python
    entry_price: float
    stop_loss: Optional[float] = None
    target: Optional[float] = None
```

- [ ] **Step 2: 新增 BuyRequest / SellRequest**

在 `ClosePositionRequest` 之后(`ArchiveRequest` 之前)插入:

```python
class BuyRequest(BaseModel):
    """买入:开仓(新代码)或加仓(已有持仓)。每笔追加 buy trade 留痕。"""
    ts_code: str
    fill_price: float
    shares: int
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    note: str = ""
    strategy: str = "manual"


class SellRequest(BaseModel):
    """卖出:减仓或卖光。卖光自动走 close_position + postmortem + calibration。"""
    ts_code: str
    fill_price: float
    shares: int
    exit_reason: str = "manual"
    note: str = ""
    postmortem: bool = True
```

- [ ] **Step 3: 语法校验**

Run: `python -c "import ast; ast.parse(open('backend/schemas/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add backend/schemas/watchlist.py
git commit -m "feat(backend): 新增 Buy/SellRequest, AddPosition 止损目标改可选

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: `backend/routers/watchlist.py` — 新增 /buy /sell /trades 路由

**Files:**
- Modify: `backend/routers/watchlist.py`

**Interfaces:**
- Consumes: `apex.watchlist.buy`/`sell`、`apex.trades.load_trades`;schemas `BuyRequest`/`SellRequest`
- Produces: `POST /api/watchlist/buy`、`POST /api/watchlist/sell`、`GET /api/watchlist/trades`

- [ ] **Step 1: 加 import**

在文件顶部 import 区(约 9-14 行),把:
```python
from apex import watchlist as wl
```
之后加一行:
```python
from apex import trades as trades_mod
```
并在 schemas import 块(约 18-25 行)的括号内加 `BuyRequest`、`SellRequest`:
```python
from backend.schemas.watchlist import (
    AddCandidateRequest,
    AddPositionRequest,
    ArchiveRequest,
    BuyRequest,
    ClosePositionRequest,
    PromoteCandidateRequest,
    ReplacePositionRequest,
    SellRequest,
)
```

- [ ] **Step 2: 加 /buy /sell /trades 路由**

在 `archive` 路由之后、`get_closed_positions` 之前(约 158 行后)插入:

```python
@router.post("/buy")
def buy(req: BuyRequest):
    """买入:开仓或加仓。返回 {position, trade}。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    return wl.buy(
        ts_code=ts_code, fill_price=req.fill_price, shares=req.shares,
        stop_loss=req.stop_loss, target=req.target,
        note=req.note, strategy=req.strategy,
    )


@router.post("/sell")
def sell(req: SellRequest):
    """卖出:减仓返回 {position, trade};卖光返回 {trade, closed_record, diagnosis}。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    return wl.sell(
        ts_code=ts_code, fill_price=req.fill_price, shares=req.shares,
        exit_reason=req.exit_reason, note=req.note, postmortem=req.postmortem,
    )


@router.get("/trades")
def list_trades(
    ts_code: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=1000),
    since_days: Optional[int] = Query(None, ge=1, le=3650),
):
    """交易流水(按 traded_at 倒序)。可按 ts_code / limit / since_days 过滤。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    return trades_mod.load_trades(ts_code=code, limit=limit, since_days=since_days)
```

- [ ] **Step 3: 语法校验**

Run: `python -c "import ast; ast.parse(open('backend/routers/watchlist.py').read())"`
Expected: 无输出。

- [ ] **Step 4: 启动后端冒烟(若环境允许)**

Run: `python -c "from backend.routers.watchlist import router; print([r.path for r in router.routes])"`
Expected: 输出包含 `/watchlist/buy`、`/watchlist/sell`、`/watchlist/trades`。

> 若 import 链报缺配置(token 等),跳过此步,Task 12 端到端再验。

- [ ] **Step 5: Commit**

```bash
git add backend/routers/watchlist.py
git commit -m "feat(backend): 新增 /watchlist/buy /sell /trades 路由

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: `frontend/src/api/query-keys.ts` + `watchlist.ts` — trades key + buy/sell/getTrades + Trade 类型

**Files:**
- Modify: `frontend/src/api/query-keys.ts`
- Modify: `frontend/src/api/watchlist.ts`

**Interfaces:**
- Produces: `qk.trades`;`buy()`/`sell()`/`getTrades()`;`Trade` 类型;`ActivePosition.avg_cost`

- [ ] **Step 1: query-keys 加 trades**

在 `frontend/src/api/query-keys.ts` 的 `qk` 对象里,`closed: ["closed"] as const,` 之后加:
```typescript
  closed: ["closed"] as const,
  trades: ["trades"] as const,
```

- [ ] **Step 2: watchlist.ts — ActivePosition 加 avg_cost**

在 `ActivePosition` interface 里,`entry_price: number;` 之后加:
```typescript
  entry_price: number;
  avg_cost?: number;
```

- [ ] **Step 3: watchlist.ts — 新增 Trade 类型 + buy/sell/getTrades**

在 `WatchlistData` interface 之后插入:

```typescript
export interface Trade {
  trade_id: string;
  ts_code: string;
  name: string;
  side: "buy" | "sell";
  fill_price: number;
  shares: number;
  amount: number;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  avg_cost_after: number | null;
  shares_after: number | null;
  strategy: string | null;
  regime: string | null;
  journal_ref: { verdict: string | null; confidence: number | null; analyzed_at: string | null } | null;
  note: string;
  traded_at: string;
}

export interface BuyPayload {
  ts_code: string;
  fill_price: number;
  shares: number;
  stop_loss?: number;
  target?: number;
  note?: string;
  strategy?: string;
}

export interface SellPayload {
  ts_code: string;
  fill_price: number;
  shares: number;
  exit_reason?: string;
  note?: string;
  postmortem?: boolean;
}

export interface BuyResponse {
  position: ActivePosition;
  trade: Trade;
}

export interface SellResponsePartial {
  position: ActivePosition;
  trade: Trade;
}

export interface SellResponseClosed {
  trade: Trade;
  closed_record: Record<string, unknown>;
  diagnosis: unknown;
}

export type SellResponse = SellResponsePartial | SellResponseClosed;
```

然后在文件末尾(api 函数区,`getWatchlist` 附近)加(注意 mock 模式直接走真后端,因为本轮后端已实现;若 `USE_MOCK` 为 true 则 mock 推入,但为简洁,mock 直接返回成功占位):

```typescript
/** POST /api/watchlist/buy */
export async function buy(payload: BuyPayload): Promise<BuyResponse> {
  return api.post("/watchlist/buy", payload);
}

/** POST /api/watchlist/sell — 减仓或卖光 */
export async function sell(payload: SellPayload): Promise<SellResponse> {
  return api.post("/watchlist/sell", payload);
}

/** GET /api/watchlist/trades — 交易流水(倒序) */
export async function getTrades(params?: {
  ts_code?: string;
  limit?: number;
  since_days?: number;
}): Promise<Trade[]> {
  const qs = new URLSearchParams();
  if (params?.ts_code) qs.set("ts_code", params.ts_code);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.since_days) qs.set("since_days", String(params.since_days));
  const q = qs.toString();
  return api.get<Trade[]>(`/watchlist/trades${q ? `?${q}` : ""}`);
}
```

> 注意:这些函数不检查 `USE_MOCK`(本轮后端已实现真接口,且 mock trades 无意义)。若项目仍以 `VITE_USE_MOCK=1` 跑前端,这些函数会请求真后端——dev 时确保后端在 8000 端口(Vite proxy 已配 `/api`→127.0.0.1:8000)。

- [ ] **Step 4: 类型校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

> 若 tsc 报无关旧错(项目可能有存量类型债),只确认本任务新增代码无错即可。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/query-keys.ts frontend/src/api/watchlist.ts
git commit -m "feat(frontend): trades key + buy/sell/getTrades + Trade 类型

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: `frontend/src/api/mutations.ts` — useBuy/useSell + 失效矩阵

**Files:**
- Modify: `frontend/src/api/mutations.ts`

**Interfaces:**
- Consumes: `buy`/`sell` from `./watchlist`;`qk`
- Produces: `useBuy()`、`useSell()`;失效矩阵加 `buy`/`sell` 行

- [ ] **Step 1: import 加 buy/sell**

把 mutations.ts 顶部 watchlist import 块改成(加 `buy`、`sell` 及其 payload/response 类型):
```typescript
import {
  addCandidate,
  addPosition,
  replacePosition,
  promoteCandidate,
  closePosition,
  archiveEntry,
  buy,
  sell,
  type AddCandidatePayload,
  type AddPositionPayload,
  type PromotePayload,
  type ClosePositionPayload,
  type ArchivePayload,
  type BuyPayload,
  type SellPayload,
  type BuyResponse,
  type SellResponse,
} from "./watchlist";
```

- [ ] **Step 2: 失效矩阵加 buy/sell**

在 `INVALIDATE` 对象里,`closePosition` 行之后加:
```typescript
  // 买入(开仓/加仓) → 持仓 + 流水(账户总风险也变)
  buy: [[...qk.watchlist], [...qk.trades], [...qk.account], [...qk.triggers]],
  // 卖出:减仓 → 持仓+流水;卖光 → 还影响 closed+calibration。统一全失效, 简单正确。
  sell: [
    [...qk.watchlist],
    [...qk.trades],
    [...qk.account],
    [...qk.closed],
    [...qk.calibration],
    [...qk.triggers],
  ],
```

- [ ] **Step 3: 加 useBuy/useSell hooks**

在 `useClosePosition` 之后插入:

```typescript
export function useBuy(): UseMutationResult<BuyResponse, Error, BuyPayload> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: buy,
    retry: false,
    onSuccess: () => invalidateAll(qc, "buy"),
  });
}

export function useSell(): UseMutationResult<SellResponse, Error, SellPayload> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: sell,
    retry: false,
    onSuccess: () => invalidateAll(qc, "sell"),
  });
}
```

- [ ] **Step 4: 类型校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/mutations.ts
git commit -m "feat(frontend): useBuy/useSell + 失效矩阵 buy/sell

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: `PositionCard.tsx` — PnL 用 avg_cost + 展示均价/股数

**Files:**
- Modify: `frontend/src/components/a-share/PositionCard.tsx`

**Interfaces:**
- Consumes: `ActivePosition`(现含 `avg_cost`);新增 props `onAdd`/`onReduce` 回调(由 WatchlistPage 传入,触发加仓/减仓表单)
- Produces: PositionCard 展示股数 + 均价,PnL 基准用 `avg_cost ?? entry_price`,加仓/减仓按钮调 `onAdd`/`onReduce`

- [ ] **Step 1: 改 props 签名 + PnL 基准**

把 `PositionCard` 函数签名与开头解构改成:

```typescript
export function PositionCard({
  position,
  onAdd,
  onReduce,
}: {
  position: ActivePosition;
  onAdd?: (p: ActivePosition) => void;
  onReduce?: (p: ActivePosition) => void;
}) {
  const { ts_code, name, entry_price, avg_cost, stop_loss, target, position_size_shares, strategy } =
    position;
```

把 PnL 计算的基准从 `entry_price` 改成 `cost = avg_cost ?? entry_price`:

```typescript
  const cost = avg_cost ?? entry_price;
  // 盈亏 = (当前价 - 成本) * 股数
  const pnl =
    currentPrice != null && position_size_shares != null
      ? (currentPrice - cost) * position_size_shares
      : null;
  const pnlDelta = currentPrice != null ? currentPrice - cost : null;
```

并把 PnL 百分比那行的 `entry_price` 也改成 `cost`:
```typescript
              ({pnlDelta != null ? ((pnlDelta / cost) * 100).toFixed(2) : "—"}%)
```

- [ ] **Step 2: 展示股数 + 均价 + 加仓/减仓按钮**

在止损/目标那行(`<div className="mt-1.5 flex items-center gap-3 ...">`)之后、`</div>`(左侧 min-w-0 结束)之前,加股数+均价行。然后在右侧价格区加按钮。具体:

把止损/目标行之后追加:
```typescript
        <div className="mt-1 flex items-center gap-3 text-[11px] text-text-secondary">
          {position_size_shares != null && (
            <span className="num">{position_size_shares} 股</span>
          )}
          {avg_cost != null && (
            <span className="num">均价 {formatPrice(avg_cost)}</span>
          )}
        </div>
```

在右侧 `<div className="text-right">` 的最末尾(`</div>` 闭合前),加加仓/减仓按钮:
```typescript
        {(onAdd || onReduce) && (
          <div className="mt-1.5 flex items-center justify-end gap-1.5">
            {onAdd && (
              <button
                type="button"
                onClick={() => onAdd(position)}
                className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
              >
                加仓
              </button>
            )}
            {onReduce && (
              <button
                type="button"
                onClick={() => onReduce(position)}
                className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
              >
                减仓
              </button>
            )}
          </div>
        )}
```

- [ ] **Step 3: 类型校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误(onAdd/onReduce 是可选 props,WatchlistPage 还没传也不报错)。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/a-share/PositionCard.tsx
git commit -m "feat(frontend): PositionCard PnL 用 avg_cost + 展示股数/均价 + 加仓减仓按钮

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 13: `WatchlistPage.tsx` — 开仓表单 + 加仓/减仓表单 + 交易流水 Card

**Files:**
- Modify: `frontend/src/routes/watchlist/WatchlistPage.tsx`

**Interfaces:**
- Consumes: `useBuy`/`useSell`/`getTrades`、`qk.trades`、`Trade`/`ActivePosition` 类型、`PositionCard` 的 `onAdd`/`onReduce`
- Produces: 持仓页有"加持仓"开仓表单、每张卡片加仓/减仓内联表单、底部交易流水 Card

- [ ] **Step 1: 改 import**

把 WatchlistPage 顶部 import 改成:

```typescript
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw, Wallet, Bell, History, ListTree } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  Button,
} from "@/components/base";
import { PositionCard, CandidateCard } from "@/components/a-share";
import { getWatchlist, getTrades, type ActivePosition, type Trade } from "@/api/watchlist";
import { useAddCandidate, useBuy, useSell } from "@/api/mutations";
import { qk } from "@/api/query-keys";
import { formatPrice } from "@/lib/utils";
```

- [ ] **Step 2: 加状态 + mutations + 流水 query**

在 `WatchlistPage` 函数体里,`addCandidateMut` 之后加:

```typescript
  const buyMut = useBuy();
  const sellMut = useSell();

  const { data: tradesData } = useQuery({
    queryKey: qk.trades,
    queryFn: () => getTrades({ limit: 50 }),
  });

  // 开仓表单
  const [showOpen, setShowOpen] = useState(false);
  const [openCode, setOpenCode] = useState("");
  const [openPrice, setOpenPrice] = useState("");
  const [openShares, setOpenShares] = useState("");
  const [openStop, setOpenStop] = useState("");
  const [openTarget, setOpenTarget] = useState("");
  const [openNote, setOpenNote] = useState("");

  // 加仓/减仓目标 + 表单
  const [tradeTarget, setTradeTarget] = useState<ActivePosition | null>(null);
  const [tradeSide, setTradeSide] = useState<"buy" | "sell">("buy");
  const [tradePrice, setTradePrice] = useState("");
  const [tradeShares, setTradeShares] = useState("");
  const [tradeNote, setTradeNote] = useState("");
```

- [ ] **Step 3: 加提交函数**

在 `submitAdd` 之后加:

```typescript
  const submitOpen = (e?: React.FormEvent) => {
    e?.preventDefault();
    const tsCode = openCode.trim().toUpperCase();
    const price = Number(openPrice);
    const shares = Number(openShares);
    if (!tsCode || !price || !shares) return;
    buyMut.mutate(
      {
        ts_code: tsCode,
        fill_price: price,
        shares,
        stop_loss: openStop ? Number(openStop) : undefined,
        target: openTarget ? Number(openTarget) : undefined,
        note: openNote,
      },
      {
        onSuccess: () => {
          setShowOpen(false);
          setOpenCode(""); setOpenPrice(""); setOpenShares("");
          setOpenStop(""); setOpenTarget(""); setOpenNote("");
        },
      },
    );
  };

  const openTradeForm = (p: ActivePosition, side: "buy" | "sell") => {
    setTradeTarget(p);
    setTradeSide(side);
    setTradePrice("");
    setTradeShares("");
    setTradeNote("");
  };

  const submitTrade = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!tradeTarget) return;
    const price = Number(tradePrice);
    const shares = Number(tradeShares);
    if (!price || !shares) return;
    const payload = {
      ts_code: tradeTarget.ts_code,
      fill_price: price,
      shares,
      note: tradeNote,
    };
    const mut = tradeSide === "buy" ? buyMut : sellMut;
    mut.mutate(payload, {
      onSuccess: () => setTradeTarget(null),
    });
  };
```

- [ ] **Step 4: 顶部按钮区改"加候选"为"加持仓"**

把顶部按钮区的"加候选"按钮换成"加持仓",并保留原"加候选"。即在 `<Button variant="primary" size="sm" onClick={() => setShowAdd((s) => !s)}>` 那个加候选按钮**之前**加一个加持仓按钮:

```typescript
          <Button
            variant="primary"
            size="sm"
            onClick={() => setShowOpen((s) => !s)}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            加持仓
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowAdd((s) => !s)}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            加候选
          </Button>
```

- [ ] **Step 5: 加开仓表单 + 加仓/减仓表单 UI**

在 `{showAdd && (...)}`(加候选表单)之前,插入开仓表单:

```typescript
      {showOpen && (
        <Card>
          <CardContent className="py-4">
            <form onSubmit={submitOpen} className="flex flex-wrap items-end gap-3">
              <div className="min-w-[140px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">代码 *</label>
                <input type="text" value={openCode} onChange={(e) => setOpenCode(e.target.value)}
                  placeholder="000001.SZ"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[100px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">成本价 *</label>
                <input type="number" step="0.01" value={openPrice} onChange={(e) => setOpenPrice(e.target.value)}
                  placeholder="12.50"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[100px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">股数 *</label>
                <input type="number" step="100" value={openShares} onChange={(e) => setOpenShares(e.target.value)}
                  placeholder="1000"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[90px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">止损</label>
                <input type="number" step="0.01" value={openStop} onChange={(e) => setOpenStop(e.target.value)}
                  placeholder="可选"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[90px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">目标</label>
                <input type="number" step="0.01" value={openTarget} onChange={(e) => setOpenTarget(e.target.value)}
                  placeholder="可选"
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[140px] flex-[2]">
                <label className="mb-1 block text-xs text-text-secondary">备注</label>
                <input type="text" value={openNote} onChange={(e) => setOpenNote(e.target.value)}
                  placeholder="可选"
                  className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <Button type="submit" variant="primary" disabled={buyMut.isPending}>
                {buyMut.isPending ? "买入中..." : "确认买入"}
              </Button>
            </form>
            {buyMut.isError && (
              <p className="mt-2 text-xs text-down">失败 · {String(buyMut.error)}</p>
            )}
          </CardContent>
        </Card>
      )}

      {tradeTarget && (
        <Card>
          <CardContent className="py-4">
            <p className="mb-3 text-sm">
              {tradeSide === "buy" ? "加仓" : "减仓"} · {tradeTarget.name} ({tradeTarget.ts_code})
              {tradeTarget.position_size_shares != null && (
                <span className="ml-2 num text-xs text-text-secondary">
                  当前 {tradeTarget.position_size_shares} 股
                </span>
              )}
            </p>
            <form onSubmit={submitTrade} className="flex flex-wrap items-end gap-3">
              <div className="min-w-[120px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">成交价 *</label>
                <input type="number" step="0.01" value={tradePrice} onChange={(e) => setTradePrice(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[120px] flex-1">
                <label className="mb-1 block text-xs text-text-secondary">股数 *</label>
                <input type="number" step="100" value={tradeShares} onChange={(e) => setTradeShares(e.target.value)}
                  className="num w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <div className="min-w-[160px] flex-[2]">
                <label className="mb-1 block text-xs text-text-secondary">备注</label>
                <input type="text" value={tradeNote} onChange={(e) => setTradeNote(e.target.value)}
                  className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm focus:border-text-secondary focus:outline-none" />
              </div>
              <Button type="submit" variant="primary"
                disabled={tradeSide === "buy" ? buyMut.isPending : sellMut.isPending}>
                确认{tradeSide === "buy" ? "加仓" : "卖出"}
              </Button>
              <Button type="button" variant="ghost" onClick={() => setTradeTarget(null)}>
                取消
              </Button>
            </form>
            {(tradeSide === "buy" ? buyMut.isError : sellMut.isError) && (
              <p className="mt-2 text-xs text-down">
                失败 · {String(tradeSide === "buy" ? buyMut.error : sellMut.error)}
              </p>
            )}
          </CardContent>
        </Card>
      )}
```

- [ ] **Step 6: PositionCard 传 onAdd/onReduce**

把持仓列表渲染处的:
```typescript
                {data.active_positions.map((p) => (
                  <PositionCard key={p.ts_code} position={p} />
                ))}
```
改成:
```typescript
                {data.active_positions.map((p) => (
                  <PositionCard
                    key={p.ts_code}
                    position={p}
                    onAdd={(pos) => openTradeForm(pos, "buy")}
                    onReduce={(pos) => openTradeForm(pos, "sell")}
                  />
                ))}
```

- [ ] **Step 7: 底部加交易流水 Card**

把页面末尾的"已平仓 / 归档"Card 之前,插入交易流水 Card:

```typescript
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <ListTree className="h-4 w-4 text-text-secondary" />
          <CardTitle>交易流水</CardTitle>
          <span className="num text-xs text-text-secondary">
            {tradesData?.length ?? 0} 条
          </span>
        </CardHeader>
        <CardContent>
          {!tradesData || tradesData.length === 0 ? (
            <p className="py-6 text-center text-sm text-flat">暂无交易记录</p>
          ) : (
            <div className="divide-y divide-border">
              {tradesData.map((t) => (
                <div key={t.trade_id} className="flex items-center justify-between gap-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${t.side === "buy" ? "bg-up/10 text-up" : "bg-down/10 text-down"}`}>
                        {t.side === "buy" ? "买入" : "卖出"}
                      </span>
                      <p className="truncate text-sm font-medium">{t.name || t.ts_code}</p>
                      <span className="num text-[11px] text-text-secondary">{t.ts_code}</span>
                    </div>
                    <p className="mt-0.5 num text-[11px] text-text-secondary">
                      {formatPrice(t.fill_price)} × {t.shares} 股 · {t.traded_at.slice(0, 16).replace("T", " ")}
                      {t.note && <span className="ml-1">· {t.note}</span>}
                    </p>
                  </div>
                  <div className="text-right">
                    {t.realized_pnl != null && (
                      <p className={`num text-sm ${t.realized_pnl >= 0 ? "text-up" : "text-down"}`}>
                        {t.realized_pnl >= 0 ? "+" : ""}{t.realized_pnl.toFixed(0)} 元
                      </p>
                    )}
                    <p className="num text-[11px] text-text-secondary">
                      {t.realized_pnl_pct != null
                        ? `${(t.realized_pnl_pct * 100).toFixed(2)}%`
                        : "—"}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
```

- [ ] **Step 8: 类型校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 9: Commit**

```bash
git add frontend/src/routes/watchlist/WatchlistPage.tsx
git commit -m "feat(frontend): 持仓页开仓/加仓/减仓表单 + 交易流水 Card

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 14: 端到端验证 + 清理

**Files:**
- 无文件改动,验证 + 清理临时脚本

- [ ] **Step 1: 后端全量语法校验**

Run:
```bash
python -c "import ast; [ast.parse(open(f).read()) for f in ['apex/trades.py','apex/watchlist.py','backend/schemas/watchlist.py','backend/routers/watchlist.py']]" && echo OK
```
Expected: `OK`

- [ ] **Step 2: 前端全量类型校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 3: 启动后端 + 前端,手动验证(若环境允许)**

后端:
```bash
uvicorn backend.main:app --port 8000 &
```
前端:
```bash
cd frontend && VITE_USE_MOCK=0 npm run dev &
```
打开 http://localhost:5173/watchlist,验证:
1. 点"加持仓" → 填代码/成本/股数 → 确认买入 → 持仓列表出现新卡片
2. 持仓卡片点"加仓" → 填价/股数 → 确认 → 卡片股数+均价更新,流水多一条买入
3. 持仓卡片点"减仓" → 填价/股数(<持有) → 确认 → 股数减少,流水多一条卖出带实现盈亏
4. 减仓到卖光 → 持仓消失,流水多一条卖出,触发复盘(若 postmortem=true)
5. 交易流水 Card 展示上述所有记录

> 若无法启动(缺 token 等),跳过手动验证,以 Task 4/6/7 的 smoke-test 为准。

- [ ] **Step 4: 清理临时脚本**

Run: `rm -f /tmp/smoke_trades.py /tmp/smoke_buy.py /tmp/smoke_sell.py /tmp/smoke_migrate.py`

- [ ] **Step 5: 更新 CLAUDE.md(可选,仅当用户要求)**

> CLAUDE.md 的「Trading flow」段提到 candidate→position 流。本轮新增手动 buy/sell 留痕,可在该段补一句。**仅在用户确认后改 CLAUDE.md**,不擅自改。

- [ ] **Step 6: 最终 commit(若有清理产生的状态变化则无,否则跳过)**

无需 commit(清理的是 /tmp)。

---

## Self-Review

**1. Spec coverage:**
- 数据模型(active_positions 加 avg_cost / trades.jsonl / closed 不变)→ Task 1,2,5
- buy 开仓/加仓 → Task 4
- sell 减仓/卖光(复用 close_position + postmortem + calibration)→ Task 6
- close_position pnl 基准改 avg_cost → Task 5
- migrate 补 avg_cost → Task 7
- schemas Buy/SellRequest + AddPosition 可选 → Task 8
- 路由 /buy /sell /trades → Task 9
- 前端 api/mutations/query-keys → Task 10,11
- PositionCard PnL + 均价/股数 + 按钮 → Task 12
- WatchlistPage 开仓/加仓/减仓表单 + 流水 Card → Task 13
- 端到端验证 → Task 14
- 全覆盖。

**2. Placeholder scan:** 无 TBD/TODO;每个步骤含完整代码或确切命令。

**3. Type consistency:**
- `buy()` 返回 `{position, trade}`;Task 9 路由直接 return;Task 10 `BuyResponse` 一致。
- `sell()` 减仓返回 `{position, trade}`,卖光返回 `{trade, closed_record, diagnosis}`;Task 10 `SellResponse` union 覆盖;Task 13 前端不区分两种返回形态(只 invalidate),OK。
- `Trade` 字段在 Task 1(python)与 Task 10(ts)对齐:trade_id/ts_code/name/side/fill_price/shares/amount/realized_pnl/realized_pnl_pct/avg_cost_after/shares_after/strategy/regime/journal_ref/note/traded_at。一致。
- `ActivePosition.avg_cost` Task 10 加,Task 12 用。一致。
- `qk.trades` Task 10 加,Task 11 用。一致。
- `onAdd`/`onReduce` Task 12 定义,Task 13 传。一致。

无问题。
