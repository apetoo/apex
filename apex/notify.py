"""推送通知 —— 触发提醒 / 止损止盈告警。

Channels: console (默认，永远可用) + bark (iOS, GET api.day.app/<key>/<title>/<body>)。
扩展新 channel：实现 _send_<name>(title, body, url, cfg) → bool 即可，并加到 _SENDERS。
"""
import json
import urllib.parse
import urllib.request

from apex import config


def notify(title: str, body: str = "", url: str = "") -> bool:
    """发送通知。优先走配置 channel，失败回落到 console。"""
    cfg = config.get().get("notify", {}) or {}
    if not cfg.get("enabled", True):
        return _send_console(title, body, url, {})

    channel = cfg.get("channel", "console")
    sender = _SENDERS.get(channel, _send_console)
    try:
        if sender(title, body, url, cfg.get(channel) or {}):
            return True
    except Exception as e:
        print(f"[notify] {channel} failed: {type(e).__name__}: {e}")

    # 任何 channel 失败时回落到 console，确保信号不丢
    return _send_console(title, body, url, {})


def _send_console(title: str, body: str, url: str, cfg: dict) -> bool:
    line = f"🔔 {title}"
    if body:
        line += f" — {body}"
    if url:
        line += f"  ({url})"
    print(line)
    return True


def _send_bark(title: str, body: str, url: str, cfg: dict) -> bool:
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        return False

    safe_title = urllib.parse.quote(title, safe="")
    safe_body = urllib.parse.quote(body, safe="")
    api_url = f"{base}/{safe_title}/{safe_body}"

    params = {"group": "apex"}
    if url:
        params["url"] = url
    api_url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(api_url, headers={"User-Agent": "apex-monitor/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return raw.get("code") == 200


_SENDERS = {
    "console": _send_console,
    "bark": _send_bark,
}
