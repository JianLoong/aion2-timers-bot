"""Plain data types shared by the store, the scheduler and the commands."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

__all__ = ["GuildPreset", "Timer", "name_key"]


def name_key(name: str) -> str:
    """Normalise a user supplied name so "World Boss" and "world  boss" collide."""
    return re.sub(r"\s+", " ", name.strip()).casefold()


@dataclass(slots=True)
class Timer:
    """One countdown, optionally repeating."""

    id: int
    guild_id: int
    channel_id: int
    name: str
    due_at: float
    created_by: int
    created_at: float
    interval_seconds: float | None = None
    note: str | None = None
    mention: str | None = None
    fired_count: int = 0

    @property
    def is_repeating(self) -> bool:
        return bool(self.interval_seconds and self.interval_seconds > 0)

    def remaining(self, now: float | None = None) -> float:
        """Seconds until this timer fires; negative once it is overdue."""
        return self.due_at - (time.time() if now is None else now)

    def is_due(self, now: float | None = None) -> bool:
        return self.remaining(now) <= 0


@dataclass(slots=True)
class GuildPreset:
    """A per-guild preset created with /timer preset add, overriding a built-in one."""

    guild_id: int
    key: str
    display_name: str
    interval_seconds: float
    category: str = "Custom"
    aliases: tuple[str, ...] = ()
    note: str | None = None
    created_by: int | None = None


@dataclass(slots=True)
class EventBoard:
    """A channel's "next up" message, refreshed as the window rolls forward."""

    guild_id: int
    channel_id: int
    message_id: int | None
    hours: float
    refresh_at: float
    created_by: int
    created_at: float


@dataclass(slots=True)
class EventReminder:
    """An opt-in warning before one scheduled event starts."""

    guild_id: int
    event_id: str
    minutes_before: int
    channel_id: int
    mention: str | None
    last_fired_at: float | None
    created_by: int
    created_at: float
