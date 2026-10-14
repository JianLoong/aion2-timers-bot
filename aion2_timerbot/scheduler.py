"""The background loop that fires timers when they come due."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from .db import Store
from .models import Timer

__all__ = ["DueHandler", "TimerScheduler", "next_occurrence"]

log = logging.getLogger(__name__)

# Called with the timer that came due and how many seconds late it is.
DueHandler = Callable[[Timer, float], Awaitable[None]]


def next_occurrence(due_at: float, interval_seconds: float, now: float) -> float:
    """The first occurrence strictly after ``now``.

    Occurrences missed while the bot was offline are skipped rather than replayed one
    tick at a time, so a 1h timer left running overnight fires once, not eight times.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    steps = math.floor((now - due_at) / interval_seconds) + 1
    return due_at + steps * interval_seconds


class TimerScheduler:
    """Polls the store for due timers and hands them to ``on_due``."""

    def __init__(
        self,
        store: Store,
        on_due: DueHandler,
        *,
        tick_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        batch_limit: int = 100,
    ) -> None:
        self.store = store
        self.on_due = on_due
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.clock = clock
        self.batch_limit = batch_limit
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="timer-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad tick must not kill the loop
                log.exception("timer scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    async def run_once(self, now: float | None = None) -> list[Timer]:
        """Fire everything that is due and advance or remove it. Returns what fired."""
        moment = self.clock() if now is None else now
        fired: list[Timer] = []
        for timer in await self.store.due_timers(moment, limit=self.batch_limit):
            lateness = max(0.0, moment - timer.due_at)
            try:
                await self.on_due(timer, lateness)
            except Exception:
                # Keep going: a deleted channel should not freeze the timer either.
                log.exception("failed to announce timer #%s (%s)", timer.id, timer.name)
            if timer.is_repeating:
                assert timer.interval_seconds is not None
                await self.store.reschedule(
                    timer.id,
                    next_occurrence(timer.due_at, timer.interval_seconds, moment),
                    fired_count=timer.fired_count + 1,
                )
            else:
                await self.store.delete_timer(timer.id)
            fired.append(timer)
        return fired
