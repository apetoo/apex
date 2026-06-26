"""响应处理助手。"""
from __future__ import annotations

import json
from typing import Any


def parse_json(raw: Any) -> Any:
    """apex/data.py 的多个工具函数返回 JSON 字符串。

    这里统一解析成 Python 对象，避免接口吐出"字符串化的 JSON"。
    解析失败时原样返回，方便上游排错。
    """
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def dataframe_records(df: Any) -> list[dict]:
    """把 pandas DataFrame 转成 records 列表。

    单独抽出便于路由层不直接依赖 pandas，且 NaN → null。
    """
    if df is None or getattr(df, "empty", False):
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))
