"""SQLite persistence for timers and per-guild presets."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import aiosqlite

from .models import GuildPreset, Timer, name_key

__all__ = ["DuplicateTimerError", "Store"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    name_key         TEXT    NOT NULL,
    due_at           REAL    NOT NULL,
    interval_seconds REAL,
    note             TEXT,
    mention          TEXT,
    created_by       INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    fired_count      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS timers_guild_name ON timers (guild_id, name_key);
CREATE INDEX IF NOT EXISTS timers_due_at ON timers (due_at);

CREATE TABLE IF NOT EXISTS guild_presets (
    guild_id         INTEGER NOT NULL,
    name_key         TEXT    NOT NULL,
    display_name     TEXT    NOT NULL,
    category         TEXT    NOT NULL DEFAULT 'Custom',
    interval_seconds REAL    NOT NULL,
    aliases          TEXT    NOT NULL DEFAULT '',
    note             TEXT,
    created_by       INTEGER,
    PRIMARY KEY (guild_id, name_key)
);
"""


class DuplicateTimerError(Exception):
    """A timer with this name already exists in the guild."""


class Store:
    """Async wrapper around one SQLite database file.

    Writes are serialised through a lock so that check-then-write sequences such as
    "insert unless the name is taken" cannot interleave.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self.path not in (":memory:", ""):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Store":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Store.open() must be awaited before using the store")
        return self._conn

    # ---------------------------------------------------------------- timers

    async def add_timer(
        self,
        *,
        guild_id: int,
        channel_id: int,
        name: str,
        due_at: float,
        created_by: int,
        interval_seconds: float | None = None,
        note: str | None = None,
        mention: str | None = None,
        created_at: float | None = None,
        replace: bool = False,
    ) -> Timer:
        key = name_key(name)
        created = time.time() if created_at is None else created_at
        async with self._lock:
            if replace:
                await self.conn.execute(
                    "DELETE FROM timers WHERE guild_id = ? AND name_key = ?", (guild_id, key)
                )
            try:
                cursor = await self.conn.execute(
                    """
                    INSERT INTO timers
                        (guild_id, channel_id, name, name_key, due_at, interval_seconds,
                         note, mention, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id, channel_id, name.strip(), key, float(due_at),
                        interval_seconds, note, mention, created_by, created,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateTimerError(name) from exc
            await self.conn.commit()
            timer_id = int(cursor.lastrowid or 0)

        timer = await self.get_timer(timer_id)
        if timer is None:  # pragma: no cover - the row was just written
            raise RuntimeError("timer disappeared right after being written")
        return timer

    async def get_timer(self, timer_id: int) -> Timer | None:
        async with self.conn.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)) as cursor:
            row = await cursor.fetchone()
        return _to_timer(row) if row is not None else None

    async def get_timer_by_name(self, guild_id: int, name: str) -> Timer | None:
        async with self.conn.execute(
            "SELECT * FROM timers WHERE guild_id = ? AND name_key = ?", (guild_id, name_key(name))
        ) as cursor:
            row = await cursor.fetchone()
        return _to_timer(row) if row is not None else None

    async def list_timers(self, guild_id: int) -> list[Timer]:
        async with self.conn.execute(
            "SELECT * FROM timers WHERE guild_id = ? ORDER BY due_at ASC, id ASC", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_timer(row) for row in rows]

    async def count_timers(self, guild_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM timers WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def due_timers(self, now: float, *, limit: int = 100) -> list[Timer]:
        async with self.conn.execute(
            "SELECT * FROM timers WHERE due_at <= ? ORDER BY due_at ASC LIMIT ?", (float(now), limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_timer(row) for row in rows]

    async def delete_timer(self, timer_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))
            await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_timer_by_name(self, guild_id: int, name: str) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM timers WHERE guild_id = ? AND name_key = ?", (guild_id, name_key(name))
            )
            await self.conn.commit()
        return cursor.rowcount > 0

    async def clear_timers(self, guild_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute("DELETE FROM timers WHERE guild_id = ?", (guild_id,))
            await self.conn.commit()
        return cursor.rowcount

    async def reschedule(self, timer_id: int, due_at: float, *, fired_count: int) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE timers SET due_at = ?, fired_count = ? WHERE id = ?",
                (float(due_at), int(fired_count), timer_id),
            )
            await self.conn.commit()

    # ------------------------------------------------------- guild presets

    async def add_guild_preset(
        self,
        *,
        guild_id: int,
        display_name: str,
        interval_seconds: float,
        category: str = "Custom",
        aliases: str | list[str] = (),
        note: str | None = None,
        created_by: int | None = None,
    ) -> GuildPreset:
        if isinstance(aliases, str):
            aliases = [part.strip() for part in aliases.split(",") if part.strip()]
        alias_text = ",".join(name_key(alias) for alias in aliases)
        key = name_key(display_name)
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO guild_presets
                    (guild_id, name_key, display_name, category, interval_seconds, aliases, note, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, name_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    category = excluded.category,
                    interval_seconds = excluded.interval_seconds,
                    aliases = excluded.aliases,
                    note = excluded.note
                """,
                (
                    guild_id, key, display_name.strip(), category.strip() or "Custom",
                    float(interval_seconds), alias_text, note, created_by,
                ),
            )
            await self.conn.commit()

        preset = await self.get_guild_preset(guild_id, display_name)
        if preset is None:  # pragma: no cover - the row was just written
            raise RuntimeError("preset disappeared right after being written")
        return preset

    async def get_guild_preset(self, guild_id: int, name: str) -> GuildPreset | None:
        async with self.conn.execute(
            """
            SELECT * FROM guild_presets
            WHERE guild_id = ?
              AND (name_key = ? OR (',' || aliases || ',') LIKE ?)
            """,
            (guild_id, name_key(name), f"%,{name_key(name)},%"),
        ) as cursor:
            row = await cursor.fetchone()
        return _to_guild_preset(row) if row is not None else None

    async def list_guild_presets(self, guild_id: int) -> list[GuildPreset]:
        async with self.conn.execute(
            "SELECT * FROM guild_presets WHERE guild_id = ? ORDER BY display_name COLLATE NOCASE", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_guild_preset(row) for row in rows]

    async def delete_guild_preset(self, guild_id: int, name: str) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM guild_presets WHERE guild_id = ? AND name_key = ?",
                (guild_id, name_key(name)),
            )
            await self.conn.commit()
        return cursor.rowcount > 0


def _to_timer(row: aiosqlite.Row) -> Timer:
    return Timer(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        name=row["name"],
        due_at=row["due_at"],
        interval_seconds=row["interval_seconds"],
        note=row["note"],
        mention=row["mention"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        fired_count=row["fired_count"],
    )


def _to_guild_preset(row: aiosqlite.Row) -> GuildPreset:
    aliases = tuple(a for a in str(row["aliases"] or "").split(",") if a)
    return GuildPreset(
        guild_id=row["guild_id"],
        key=row["name_key"],
        display_name=row["display_name"],
        interval_seconds=row["interval_seconds"],
        category=row["category"],
        aliases=aliases,
        note=row["note"],
        created_by=row["created_by"],
    )
