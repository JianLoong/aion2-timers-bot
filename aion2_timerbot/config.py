"""Configuration read from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Config", "ConfigError"]


class ConfigError(Exception):
    """Raised when the environment does not describe a runnable bot."""


@dataclass(slots=True)
class Config:
    token: str
    db_path: Path = Path("data/timers.db")
    presets_file: Path | None = None
    dev_guild_id: int | None = None
    tick_seconds: float = 5.0
    missed_after_seconds: float = 120.0
    max_timers_per_guild: int = 100
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        token = (env.get("DISCORD_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and paste your "
                "bot token from https://discord.com/developers/applications."
            )

        presets_file = (env.get("PRESETS_FILE") or "").strip()

        return cls(
            token=token,
            db_path=Path((env.get("DB_PATH") or "data/timers.db").strip()),
            presets_file=Path(presets_file) if presets_file else None,
            dev_guild_id=_optional_int(env, "DEV_GUILD_ID"),
            tick_seconds=_float(env, "TICK_SECONDS", default=5.0, minimum=1.0),
            missed_after_seconds=_float(env, "MISSED_AFTER_SECONDS", default=120.0, minimum=0.0),
            max_timers_per_guild=_int(env, "MAX_TIMERS_PER_GUILD", default=100, minimum=1),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        )


def _optional_int(env: dict[str, str], name: str) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a Discord ID (a number), got {raw!r}.") from exc


def _int(env: dict[str, str], name: str, *, default: int, minimum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}.")
    return value


def _float(env: dict[str, str], name: str, *, default: float, minimum: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}.")
    return value
