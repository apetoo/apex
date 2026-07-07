"""持仓推送管理端点。

这些是 apex 自己暴露的 HTTP 端点（供前端/运维触发），不是消费方实现的：
  POST /api/push/full          手动触发一次全量推送
  GET  /api/push/status        推送当前状态（开关 / last_seq / 末次结果）
  GET  /api/push/log?limit=N   最近 N 条推送审计日志
  POST /api/push/replay?since  重放 seq > since 的推送（从 push_payloads/ 读 envelope 重发）

接口契约见 docs/integration/positions-push-api.md。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from apex import push as push_mod

router = APIRouter(prefix="/push", tags=["push"])


@router.post("/full")
def trigger_full():
    """手动触发一次全量推送。同步返回发送结果。"""
    return push_mod.push_full()


@router.get("/status")
def get_status():
    """推送当前状态。"""
    return push_mod.status()


@router.get("/log")
def get_log(limit: int = Query(100, ge=1, le=1000)):
    """最近 N 条推送日志（按 seq 倒序）。"""
    return push_mod.load_log(limit=limit)


@router.post("/replay")
def replay(since: int = Query(0, ge=0)):
    """重放 seq > since 的推送。"""
    return push_mod.replay(since=since)
