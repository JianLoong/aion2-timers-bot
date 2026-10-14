"""Where the schedule comes from: the public timers.json, a local cache, and your overrides.

The remote file is third-party (shugo.gg publishes it, and notes that NCSOFT never published
the Global times), so it is never trusted as the only source: a fetched copy is cached to disk,
a failed fetch falls back to that cache, and an override file can patch or add events — which
is how you fix a time that is wrong on your server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .schedule import (
    Region,
    Schedule,
    ScheduleError,
    merge_override,
    parse_override,
    parse_timers,
)

__all__ = ["DEFAULT_SCHEDULE_URL", "ScheduleSource", "download"]

log = logging.getLogger(__name__)

DEFAULT_SCHEDULE_URL = "https://shugo.gg/timers.json"
DEFAULT_TTL_SECONDS = 6 * 3600.0
FETCH_TIMEOUT = 20.0
# After a failed fetch, wait this long before trying again (not the full TTL).
RETRY_SECONDS = 5 * 60.0
USER_AGENT = "aion2-timerbot (Discord timer bot)"


def download(url: str, timeout: float) -> bytes:
    """Fetch a URL. Pulled out so tests can swap in a stub instead of hitting the network."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class ScheduleSource:
    """Loads and caches the event schedule for one region."""

    def __init__(
        self,
        *,
        region: str,
        url: str = DEFAULT_SCHEDULE_URL,
        cache_path: str | Path | None = None,
        override_path: str | Path | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = FETCH_TIMEOUT,
        opener: Callable[[str, float], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.region = region
        self.url = (url or "").strip()
        self.cache_path = Path(cache_path) if cache_path else None
        self.override_path = Path(override_path) if override_path else None
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.timeout = timeout
        self.clock = clock

        self._opener = opener or download
        self._lock = asyncio.Lock()
        self._payload: dict[str, Any] | None = None
        self._schedule: Schedule | None = None
        self._loaded_at: float | None = None
        self._override_stamp: tuple[float, int] | None = None

        self.last_error: str | None = None
        self.data_error: str | None = None
        self.override_error: str | None = None

    # ------------------------------------------------------------------ public

    @property
    def problem(self) -> str | None:
        """The one thing worth telling the user, worst first."""
        return self.data_error or self.last_error or self.override_error

    @property
    def schedule(self) -> Schedule | None:
        """Whatever is loaded right now; None until the first ``get()``."""
        return self._schedule

    async def get(self, *, force: bool = False) -> Schedule:
        """The schedule, refreshed when stale, cached when the network is down.

        Also notices edits to the override file, so fixing a time takes effect at once.
        """
        async with self._lock:
            now = self.clock()
            if force or self._schedule is None or self._is_stale(now):
                await self._reload(now)
            elif self._override_changed():
                self._schedule = self._build()
            return self._schedule

    def _is_stale(self, now: float) -> bool:
        if self._loaded_at is None:
            return True
        return (now - self._loaded_at) >= self.ttl_seconds

    # ----------------------------------------------------------------- loading

    async def _reload(self, now: float) -> None:
        if self.url:
            payload = await self._fetch()
            if payload is not None:
                self._payload = payload
                self.last_error = None
                self._loaded_at = now
                self._write_cache(payload)
            else:
                # Do not hammer them: try again in RETRY_SECONDS, not the whole TTL.
                self._loaded_at = now - max(0.0, self.ttl_seconds - RETRY_SECONDS)
                if self._payload is None:
                    self._payload = self._read_cache()
        else:
            if self._payload is None:
                self._payload = self._read_cache()
            self._loaded_at = now

        self._schedule = self._build()

    async def _fetch(self) -> dict[str, Any] | None:
        try:
            raw = await asyncio.to_thread(self._opener, self.url, self.timeout)
        except Exception as exc:  # network error, DNS, TLS, timeout - all of them are "no data"
            self.last_error = f"could not fetch {self.url}: {exc}"
            log.warning("%s", self.last_error)
            return None

        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.last_error = f"{self.url} did not return JSON: {exc}"
            log.warning("%s", self.last_error)
            return None

        if not isinstance(payload, dict):
            self.last_error = f"{self.url} returned {type(payload).__name__}, expected an object"
            log.warning("%s", self.last_error)
            return None
        self.last_error = None
        return payload

    def _build(self) -> Schedule:
        """Remote (or cached) data plus local overrides. Never raises: errors land in last_error."""
        override_payload = self._read_override()
        schedule: Schedule | None = None

        if self._payload is not None:
            try:
                schedule = parse_timers(self._payload, self.region)
                self.data_error = None
            except ScheduleError as exc:
                # Not a fetch problem: the data itself cannot be used.
                self.data_error = str(exc)
                log.warning("schedule data unusable: %s", exc)

        if schedule is None:
            schedule = self._base_from_override(override_payload)
            if schedule is None:
                # Nothing usable: an empty schedule keeps the bot running and the board says so.
                schedule = Schedule(region=Region(id=self.region, label=self.region, time_zone="UTC"))

        if override_payload is None:
            return schedule

        try:
            overrides = parse_override(override_payload, schedule.region.id)
            merged = merge_override(schedule, overrides)
        except ScheduleError as exc:
            self.override_error = str(exc)
            log.warning("schedule override ignored: %s", exc)
            return schedule

        self.override_error = None
        return merged

    def _base_from_override(self, payload: object) -> Schedule | None:
        """Build a schedule from the override file alone, for a bot with no network at all."""
        if not isinstance(payload, dict):
            return None
        zone = str(payload.get("timeZone") or "").strip()
        if not zone:
            return None
        region_id = str(payload.get("region") or self.region).strip() or self.region
        return Schedule(
            region=Region(
                id=region_id,
                label=str(payload.get("regionLabel") or region_id).strip(),
                time_zone=zone,
            )
        )

    # ------------------------------------------------------------------- cache

    def _read_cache(self) -> dict[str, Any] | None:
        path = self.cache_path
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read the schedule cache %s: %s", path, exc)
            return None
        payload = raw.get("payload") if isinstance(raw, dict) else None
        if isinstance(payload, dict):
            log.info("using the cached schedule in %s", path)
            return payload
        return None

    def _write_cache(self, payload: dict[str, Any]) -> None:
        path = self.cache_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"fetched_at": self.clock(), "url": self.url, "payload": payload},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write the schedule cache %s: %s", path, exc)

    # ---------------------------------------------------------------- override

    def _read_override(self) -> object | None:
        path = self.override_path
        if path is None:
            return None
        try:
            stat = path.stat()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._override_stamp = None
            self.override_error = None
            return None
        except (OSError, json.JSONDecodeError) as exc:
            self.override_error = f"{path.name}: {exc}"
            log.warning("could not read the schedule override %s: %s", path, exc)
            return None
        self._override_stamp = (stat.st_mtime, stat.st_size)
        return payload

    def _override_changed(self) -> bool:
        if self.override_path is None:
            return False
        try:
            stat = self.override_path.stat()
        except OSError:
            return self._override_stamp is not None  # deleted since we last read it
        return (stat.st_mtime, stat.st_size) != self._override_stamp
