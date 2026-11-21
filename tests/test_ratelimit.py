"""C2, F2, F16: the limiter never exceeds the RPM setting in any 60 s window (simulated 10 minutes)."""
import asyncio

from promoscout.ratelimit import RateLimiter
from promoscout.settings import Settings


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


def max_in_window(times, window=60.0):
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


async def _drive(lim, clock, minutes, workers=3):
    stamps = []

    async def worker():
        while clock.t < minutes * 60:
            await lim.acquire()
            if clock.t < minutes * 60:
                stamps.append(clock.t)
            await clock.sleep(0.5)  # simulated response time

    await asyncio.gather(*(worker() for _ in range(workers)))
    return sorted(stamps)


async def test_ten_minutes_never_exceeds_rpm():
    clock = FakeClock()
    lim = RateLimiter(30, (100, 400), clock=clock, sleep=clock.sleep)
    stamps = await _drive(lim, clock, 10)
    assert len(stamps) > 250                      # it actually ran near the limit
    assert max_in_window(stamps) <= 30


async def test_ceiling_clamped_to_200():
    assert Settings(max_rpm=5000).max_rpm == 200
    clock = FakeClock()
    lim = RateLimiter(5000, (0, 0), clock=clock, sleep=clock.sleep)
    assert lim.max_rpm == 200
    stamps = await _drive(lim, clock, 3, workers=10)
    assert max_in_window(stamps) <= 200


async def test_pushback_halves_and_floor():
    clock = FakeClock()
    lim = RateLimiter(30, (0, 0), clock=clock, sleep=clock.sleep)
    lim.server_pushback()
    assert lim.rate == 15
    for _ in range(5):
        lim.server_pushback()
    assert lim.rate == 10  # floor


async def test_slowdown_and_gradual_restore():
    clock = FakeClock()
    lim = RateLimiter(40, (0, 0), slow_factor=3, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        assert not lim.record_response(1.0)
    assert lim.record_response(3.5)          # > 3x median of the same page type
    assert lim.rate == 20 and lim.slow_reason
    for _ in range(20):
        lim.record_response(1.0)
    assert 20 < lim.rate < 40                # restored gradually, not at once
    for _ in range(60):
        lim.record_response(1.0)
    assert lim.rate == 40 and lim.slow_reason is None


async def test_retry_after_pause_honored():
    clock = FakeClock()
    lim = RateLimiter(30, (0, 0), clock=clock, sleep=clock.sleep)
    await lim.acquire()
    lim.pause_for(90)
    await lim.acquire()
    assert clock.t >= 90


async def test_slowdown_compares_same_page_type_only():
    clock = FakeClock()
    lim = RateLimiter(30, (0, 0), slow_factor=3, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        lim.record_response(0.2, "sitemap")
    assert not lim.record_response(1.5, "listing")      # big page vs tiny sitemap: not a slowdown
    for _ in range(3):
        assert not lim.record_response(2.0, "listing")  # fewer than 10 listing samples: no decision
    assert lim.rate == 30
