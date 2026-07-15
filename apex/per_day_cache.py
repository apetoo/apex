"""按日进程级缓存（A3 / T3）。

市场级信号/数据 fetcher 是按 trade_date 的全市场单日调用，限频且昂贵。Playstyle FE
的个股多日需求（主力净流入近 5 日、连板近 10 日等）+ D8 稳定性 re-run 会反复命中同一
trade_date，按 (name, trade_date) 缓存使 re-run 近零成本，解除限频瓶颈。

设计要点：
- **进程级、无 TTL**：历史交易日数据不变；当日盘后 analyze 亦稳定可复现（success
  criteria「可复现」要求给定行情输出确定）。新进程重新拉取，不做跨进程落盘（那是
  market_cache.py 的 per-stock parquet 职责，与此正交）。
- **含空结果缓存**：`[]` / `"[]""` 是合法结果（如当日无涨停），缓存它避免反复拉取；
  代价是 fetcher fail-soft 把限频失败也转成空时会被缓存——罕见且可用 clear_cache() 复原，
  换取 re-run 确定性（v1 取舍）。
- **不缓存逃逸异常**：fetcher 未捕获的异常不落缓存，下次重试。
- **fetch 不持全局锁**：不同 trade_date 可并发拉取；同 key 并发最多重复拉一次（幂等）。

应用对象（T3）：data.get_moneyflow / signals.{moneyflow,northbound,limit_up}.fetch。
"""
import threading
from functools import wraps
from typing import Callable, Optional

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], object] = {}
_SENTINEL = object()


def _norm_date(trade_date: str) -> str:
    return (trade_date or "").replace("-", "").strip()


def per_day_cache(name: str) -> Callable:
    """装饰器：按 (name, trade_date) 缓存 ``fetch(trade_date, ...)`` 的返回值。

    被装饰函数必须以 trade_date 为首个位置参数（其余参数原样透传，但不参与缓存键——
    仅用于 ``fetch(trade_date)`` 形态的 market-wide 单日 fetcher）。
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(trade_date: str, *args, **kwargs):
            key = (name, _norm_date(trade_date))
            cached = _CACHE.get(key, _SENTINEL)
            if cached is not _SENTINEL:
                return cached
            # fetch 不持锁：不同日期可并发；同 key 并发至多重复拉一次（幂等，落盘一致）
            value = fn(trade_date, *args, **kwargs)
            with _LOCK:
                _CACHE[key] = value
            return value

        wrapper._per_day_cache_name = name  # type: ignore[attr-defined]
        return wrapper

    return decorator


def clear_cache(name: Optional[str] = None) -> int:
    """清缓存。name=None 清全部，否则只清该 name。返回清除条数。"""
    with _LOCK:
        if name is None:
            n = len(_CACHE)
            _CACHE.clear()
            return n
        keys = [k for k in _CACHE if k[0] == name]
        for k in keys:
            del _CACHE[k]
        return len(keys)


def cache_stats() -> dict[str, int]:
    """返回 {name: 条数} 统计，供调试/测试。"""
    with _LOCK:
        out: dict[str, int] = {}
        for (name, _d) in _CACHE:
            out[name] = out.get(name, 0) + 1
        return out
