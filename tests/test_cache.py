from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nonebot_plugin_weather_fighter.services import cache as cache_module
from nonebot_plugin_weather_fighter.services.cache import BucketCache, CachedFailure


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(cache_module, "time", fake)
    return fake


async def test_bucket_hit_within_same_bucket(clock: Clock) -> None:
    clock.now = 1020.0
    cache: BucketCache[int] = BucketCache(bucket=60, stale=3600, timeout=1)
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert (await cache.get("k", fetch)).value == 1
    clock.now += 30
    assert (await cache.get("k", fetch)).value == 1
    assert calls == 1

    clock.now += 31
    assert (await cache.get("k", fetch)).value == 2
    assert calls == 2


async def test_single_flight(clock: Clock) -> None:
    cache: BucketCache[int] = BucketCache(bucket=60, stale=3600, timeout=1)
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return 42

    first, second = await asyncio.gather(cache.get("k", fetch), cache.get("k", fetch))
    assert first.value == 42
    assert second.value == 42
    assert calls == 1


async def test_stale_value_and_negative_cache(clock: Clock) -> None:
    cache: BucketCache[str] = BucketCache(bucket=60, stale=300, timeout=1, negative=60)
    calls = 0

    async def ok() -> str:
        nonlocal calls
        calls += 1
        return "old"

    assert (await cache.get("k", ok)).value == "old"
    clock.now += 61

    async def fail() -> str:
        raise RuntimeError("upstream down")

    stale = await cache.get("k", fail)
    assert stale.value == "old"
    assert stale.stale is True

    with pytest.raises(CachedFailure):
        await cache.get("k", ok)
    assert calls == 1

    clock.now += 61
    assert (await cache.get("k", ok)).value == "old"
    assert calls == 2


async def test_failure_without_previous_value(clock: Clock) -> None:
    cache: BucketCache[int] = BucketCache(bucket=60, stale=300, timeout=1, negative=60)
    calls = 0

    async def fail() -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await cache.get("k", fail)
    with pytest.raises(CachedFailure):
        await cache.get("k", fail)
    assert calls == 1


async def test_zero_bucket_always_fetches(clock: Clock) -> None:
    cache: BucketCache[int] = BucketCache(bucket=0, stale=3600, timeout=1)
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert (await cache.get("k", fetch)).value == 1
    assert (await cache.get("k", fetch)).value == 2
    assert calls == 2


async def test_timeout(clock: Clock) -> None:
    cache: BucketCache[int] = BucketCache(bucket=60, stale=300, timeout=0.01)

    async def slow() -> int:
        await asyncio.sleep(0.05)
        return 1

    with pytest.raises(asyncio.TimeoutError):
        await cache.get("k", slow)


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        BucketCache[Any](bucket=-1, stale=1, timeout=1)
    with pytest.raises(ValueError):
        BucketCache[Any](bucket=1, stale=-1, timeout=1)
    with pytest.raises(ValueError):
        BucketCache[Any](bucket=1, stale=1, timeout=0)
