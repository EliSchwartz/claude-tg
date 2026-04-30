from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    pass


_VALID_FAILURE_MODES = ("deny", "approve", "ask_cli")


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_supergroup_id: int
    allowed_user_ids: list[int]
    reply_timeout_sec: int = 0
    on_telegram_failure: str = "deny"
    heartbeat_interval_sec: int = 30
    idle_threshold_sec: int = 120


def _require(raw: dict[str, Any], key: str, type_: type) -> Any:
    if key not in raw:
        raise ConfigError(f"missing required field: {key}")
    val = raw[key]
    if not isinstance(val, type_):
        raise ConfigError(f"{key} must be {type_.__name__}, got {type(val).__name__}")
    return val


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)

    token = _require(raw, "telegram_bot_token", str)
    group = _require(raw, "telegram_supergroup_id", int)
    users = _require(raw, "allowed_user_ids", list)
    if not all(isinstance(u, int) for u in users):
        raise ConfigError("allowed_user_ids must be a list of integers")

    failure = raw.get("on_telegram_failure", "deny")
    if failure not in _VALID_FAILURE_MODES:
        raise ConfigError(
            f"on_telegram_failure must be one of {_VALID_FAILURE_MODES}, got {failure!r}"
        )

    return Config(
        telegram_bot_token=token,
        telegram_supergroup_id=group,
        allowed_user_ids=list(users),
        reply_timeout_sec=int(raw.get("reply_timeout_sec", 0)),
        on_telegram_failure=failure,
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        idle_threshold_sec=int(raw.get("idle_threshold_sec", 120)),
    )
