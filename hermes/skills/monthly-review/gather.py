#!/usr/bin/env python3
"""apex 月度复盘数据采集 —— 调本地后端，汇总成一份 JSON 给 Hermes 写月报。

只做：读 + 触发 recompute（calibration/system）+ 对缺 diagnosis 的平仓跑 postmortem。
不做：不改 config、不动持仓、不下单、不 promote/buy/sell。

用法:
    APEX_API=http://127.0.0.1:8000/api MONTH_DAYS=30 python gather.py
    WITH_AI_REVIEW=1 python gather.py     # 额外跑 /backtest/review（贵，调 DeepSeek）

输出:
    stdout  一份 JSON（Hermes 读它写月报）
    落盘    <repo>/reports/monthly-YYYY-MM.json
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = os.environ.get("APEX_API", "http://127.0.0.1:8000/api").rstrip("/")
DAYS = int(os.environ.get("MONTH_DAYS", "30"))
WITH_AI_REVIEW = os.environ.get("WITH_AI_REVIEW", "0") == "1"


def _req(method: str, path: str, body=None, timeout: int = 120):
    url = f"{API}{path}"
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_body": e.read().decode()[:500]}
    except Exception as e:  # 连接拒绝 / 超时 / JSON 解析失败
        return {"_error": f"{type(e).__name__}: {e}"}


def _recompute(post_path: str, get_path: str):
    """先 POST 重算，失败/空再回退 GET 读旧值。"""
    res = _req("POST", post_path)
    if not res or (isinstance(res, dict) and res.get("_error")):
        res = _req("GET", get_path)
    return res


def _err(x) -> str | None:
    return x.get("_error") if isinstance(x, dict) else None


def main():
    out: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "api_base": API,
        "window_days": DAYS,
    }

    # 1. 窗口内已平仓记录
    closed = _req("GET", f"/watchlist/closed?since_days={DAYS}")
    out["closed"] = closed

    # 2. 对缺 diagnosis 的平仓跑 AI 复盘（每笔调一次 DeepSeek，可能慢）
    pms = []
    if isinstance(closed, list):
        for rec in closed:
            if rec.get("diagnosis"):
                continue
            closed_at = (rec.get("close") or {}).get("closed_at")
            if not closed_at:
                continue
            res = _req("POST", "/postmortem/run", {"closed_at": closed_at}, timeout=300)
            pms.append({
                "closed_at": closed_at,
                "ts_code": rec.get("ts_code"),
                "name": rec.get("name"),
                "ok": not _err(res),
                "diagnosis": (res or {}).get("diagnosis") if isinstance(res, dict) else res,
                "error": _err(res),
            })
    out["postmortems_run"] = pms

    # 3. AI 校准（verdict×confidence 桶胜率）
    out["calibration"] = _recompute("/calibration/recompute", "/calibration")

    # 4. 交易系统视图（Trading DNA / Behavior / Discipline）
    out["system"] = _recompute("/system/recompute", "/system")

    # 5. 实际平仓分析（含佣金/印花税）
    out["realized"] = _req("GET", "/backtest/realized")

    # 6. 信号模拟回测 + 组合净值
    out["portfolio"] = _req("GET", "/backtest/portfolio?lookforward_days=10")
    out["signals"] = _req("GET", "/backtest/signals?lookforward_days=10")

    # 7. 窗口内交易流水
    out["trades"] = _req("GET", f"/watchlist/trades?since_days={DAYS}")

    # 8. 可选：AI 复盘整批信号（贵）
    if WITH_AI_REVIEW:
        out["ai_review"] = _req("POST", "/backtest/review", {"lookforward_days": 10}, timeout=600)
    else:
        out["ai_review"] = None

    # 汇总错误
    errors = []
    for k, v in out.items():
        e = _err(v)
        if e:
            errors.append(f"{k}: {e}")
    out["errors"] = errors

    # 落盘 + stdout
    repo = Path(__file__).resolve().parents[3]  # hermes/skills/monthly-review/ -> 仓库根
    reports = repo / "reports"
    reports.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m")
    fjson = reports / f"monthly-{stamp}.json"
    fjson.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    out["_saved"] = str(fjson)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    if errors:
        print(f"\n[gather] ⚠ {len(errors)} 个端点失败: {errors}", file=os.stderr)


if __name__ == "__main__":
    main()
