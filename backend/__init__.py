"""Apex FastAPI 后端。

薄路由层：解析请求 → 规范化 ts_code → 调 apex.* 服务 → 返回 JSON。
启动：``uvicorn backend.main:app --reload --port 8000``
"""
