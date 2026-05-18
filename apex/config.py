import logging
import os
import yaml
from pathlib import Path

_cfg: dict = {}
_logger = logging.getLogger(__name__)


def load(path: str = "config.yaml") -> dict:
    global _cfg
    config_path = Path(path)
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        _cfg = yaml.safe_load(f)

    # Tokens: yaml file takes priority; fall back to env var if yaml value is empty.
    def _resolve(section: str, key: str, env_var: str):
        yaml_val = _cfg.setdefault(section, {}).get(key, "")
        _cfg[section][key] = yaml_val or os.environ.get(env_var, "")

    _resolve("tushare", "token", "TUSHARE_TOKEN")
    _resolve("deepseek", "api_key", "DEEPSEEK_API_KEY")
    _resolve("bocha", "api_key", "BOCHA_API_KEY")

    # Tilde expansion for all path fields
    paths = _cfg.setdefault("paths", {})
    for key, val in paths.items():
        paths[key] = str(Path(val).expanduser())

    _apply_proxy(_cfg.setdefault("proxy", {}))

    return _cfg


def _apply_proxy(proxy_cfg: dict) -> None:
    """把 config.proxy 同步到 HTTP_PROXY / HTTPS_PROXY / NO_PROXY 环境变量（大小写两份都写）。

    优先级：shell 环境变量 > config.yaml。
    设置后 httpx / requests / urllib 会自动生效，无需各模块单独传参。
    新浪行情接口在 data.get_realtime_price 内显式 ProxyHandler({}) bypass，不受影响。
    """
    mapping = {
        "http_proxy": ("HTTP_PROXY", "http_proxy"),
        "https_proxy": ("HTTPS_PROXY", "https_proxy"),
        "no_proxy": ("NO_PROXY", "no_proxy"),
    }
    applied = {}
    for cfg_key, (upper, lower) in mapping.items():
        existing = os.environ.get(upper) or os.environ.get(lower)
        cfg_val = ((proxy_cfg or {}).get(cfg_key) or "").strip()
        chosen = existing or cfg_val
        if not chosen:
            continue
        # 大小写两种形式都写，避免只查 HTTPS_PROXY 或只查 https_proxy 的库读不到
        os.environ[upper] = chosen
        os.environ[lower] = chosen
        applied[upper] = chosen
    if applied:
        _logger.info("代理已启用: %s", ", ".join(f"{k}={v}" for k, v in applied.items()))


def get() -> dict:
    if not _cfg:
        load()
    return _cfg
