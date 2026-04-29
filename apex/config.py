import os
import yaml
from pathlib import Path

_cfg: dict = {}


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

    return _cfg


def get() -> dict:
    if not _cfg:
        load()
    return _cfg
