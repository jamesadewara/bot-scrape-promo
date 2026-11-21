"""Single token bucket shared by every request (F2, C2) plus adaptive slowdown (F16).

Bucket capacity is 1 token, so requests are evenly spaced at 60/rpm seconds and no
60-second window can ever hold more than `rpm` requests. A sliding-window guard
enforces the configured ceiling as a second, independent check.
"""
from __future__ import annotations

import asyncio
import collections
import random
import statistics
import time
from typing import Any, Awaitable, Callable

from .settings import RPM_CEILING, RPM_FLOOR


class RateLimiter:
    def __init__(
        self,
        max_rpm: int,
        jitter_ms: tuple[int, int] = (100, 400),
        slow_factor: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.max_rpm = max(RPM_FLOOR, min(max_rpm, RPM_CEILING))
        self.rate = float(self.max_rpm)  # current effective rpm
        self.jitter_ms = jitter_ms
        self.slow_factor = slow_factor
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._window: collections.deque[float] = collections.deque()
        self._pause_until = 0.0
        self._pushback_until = 0.0
        self._slow_reason: str | None = None
        self._normal_streak = 0
        self._latencies: list[float] = []
        self._by_kind: dict[str, list[float]] = {}
        self.total_acquired = 0

    # ------------------------------------------------------------------ acquire
    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                wait = max(self._next_at - now, self._pause_until - now)
                # sliding window guard: never more than max_rpm in any 60 s
                while self._window and now - self._window[0] >= 60:
                    self._window.popleft()
                if len(self._window) >= self.max_rpm:
                    wait = max(wait, 60 - (now - self._window[0]) + 0.001)
                if wait <= 0:
                    break
                await self._sleep(wait)
            now = self._clock()
            self._window.append(now)
            self.total_acquired += 1
            jitter = random.uniform(*self.jitter_ms) / 1000.0
            self._next_at = now + 60.0 / self.rate + jitter

    # ---------------------------------------------------------------- feedback
    def pause_for(self, seconds: float) -> None:
        """Retry-After / network pause: nothing is sent until this passes."""
        self._pause_until = max(self._pause_until, self._clock() + seconds)

    def server_pushback(self, minutes: float = 5) -> None:
        """A single 429/503: halve the rate for the next 5 minutes (2.2.1)."""
        self._halve("server asked for less traffic")
        self._pushback_until = self._clock() + minutes * 60

    MIN_SAMPLES = 10  # no slowdown decision before 10 responses of the same page type

    def record_response(self, latency_s: float, kind: str = "page") -> bool:
        """Feed a response time. Returns True if this response triggered a slowdown.

        The median is taken per page type: a 6 KB robots.txt or sitemap answers far faster than a
        900 KB listing page, so mixing them would trigger false slowdowns.
        """
        hist = self._by_kind.setdefault(kind, [])
        median = statistics.median(hist) if len(hist) >= self.MIN_SAMPLES else None
        hist.append(latency_s)
        if len(hist) > 500:
            del hist[:-500]
        self._latencies.append(latency_s)
        if len(self._latencies) > 500:
            self._latencies = self._latencies[-500:]
        if median and latency_s > self.slow_factor * median:
            self._halve(f"responses {self.slow_factor:g} times normal")
            return True
        self._normal_streak += 1
        if self._normal_streak >= 20 and self.rate < self.max_rpm and self._clock() >= self._pushback_until:
            # restore gradually: +25% of the ceiling per 20 normal responses
            self.rate = min(self.max_rpm, self.rate + max(1.0, self.max_rpm * 0.25))
            self._normal_streak = 0
            if self.rate >= self.max_rpm:
                self._slow_reason = None
        return False

    def _halve(self, reason: str) -> None:
        self.rate = max(float(RPM_FLOOR), self.rate / 2)
        self._normal_streak = 0
        self._slow_reason = reason
        # apply immediately to the next slot
        self._next_at = max(self._next_at, self._clock() + 60.0 / self.rate)

    @property
    def slowed(self) -> bool:
        return self.rate < self.max_rpm

    @property
    def slow_reason(self) -> str | None:
        return self._slow_reason if self.slowed else None

    @property
    def median_latency(self) -> float | None:
        return statistics.median(self._latencies) if self._latencies else None

    def requests_in_last_minute(self) -> int:
        now = self._clock()
        return sum(1 for t in self._window if now - t < 60)
