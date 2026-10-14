"""Configuration read from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .schedule_source import DEFAULT_SCHEDULE_URL

__all__ = ["Config", "ConfigError"]

# Which server clock the event schedule follows. "global-as" (Asia) is what shugo.gg's page
# defaults to; the events it lists are the same wall-clock hours in every region.
DEFAULT_SCHEDULE_REGION = "global-as"


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
    # event schedules (the /timer board feature)
    schedule_region: str = DEFAULT_SCHEDULE_REGION
    schedule_url: str = DEFAULT_SCHEDULE_URL
    schedule_ttl_hours: float = 6.0
    schedules_file: Path | None = None

    @property
    def schedules_path(self) -> Path:
        """Your override file: alongside the database unless SCHEDULES_FILE says otherwise."""
        return self.schedules_file or self.db_path.parent / "schedules.json"

    @property
    def schedule_cache_path(self) -> Path:
        """Where the last good copy of the remote schedule is kept."""
        return self.db_path.parent / "schedule-cache.json"

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
        schedules_file = (env.get("SCHEDULES_FILE") or "").strip()

        return cls(
            token=token,
            db_path=Path((env.get("DB_PATH") or "data/timers.db").strip()),
            presets_file=Path(presets_file) if presets_file else None,
            dev_guild_id=_optional_int(env, "DEV_GUILD_ID"),
            tick_seconds=_float(env, "TICK_SECONDS", default=5.0, minimum=1.0),
            missed_after_seconds=_float(env, "MISSED_AFTER_SECONDS", default=120.0, minimum=0.0),
            max_timers_per_guild=_int(env, "MAX_TIMERS_PER_GUILD", default=100, minimum=1),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
            schedule_region=(env.get("SCHEDULE_REGION") or DEFAULT_SCHEDULE_REGION).strip(),
            schedule_url=(env.get("SCHEDULE_URL") or DEFAULT_SCHEDULE_URL).strip(),
            schedule_ttl_hours=_float(env, "SCHEDULE_TTL_HOURS", default=6.0, minimum=0.0),
            schedules_file=Path(schedules_file) if schedules_file else None,
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
