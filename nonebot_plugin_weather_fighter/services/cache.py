from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheValue(Generic[T]):
    value: T
    stale: bool = False


class CachedFailure(RuntimeError):
    def __init__(self, key: str, retry_after: float) -> None:
        super().__init__(f"上游请求失败，{max(0.0, retry_after):.0f}s 内不再重试：{key}")
        self.key = key
        self.retry_after = retry_after


class BucketCache(Generic[T]):
    """时间段桶缓存 + 单飞 + 负缓存 + 旧值兜底。"""

    def __init__(
        self,
        *,
        bucket: int,
        stale: int,
        timeout: float,
        negative: int = 60,
    ) -> None:
        if bucket < 0:
            raise ValueError("bucket 不能为负")
        if stale < 0:
            raise ValueError("stale 不能为负")
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if negative < 0:
            raise ValueError("negative 不能为负")
        self.bucket = bucket
        self.stale = stale
        self.timeout = timeout
        self.negative = negative
        self._mem: dict[str, tuple[float, T]] = {}
        self._inflight: dict[str, asyncio.Task[CacheValue[T]]] = {}
        self._fail_until: dict[str, float] = {}

    async def get(self, key: str, fetch: Callable[[], Awaitable[T]]) -> CacheValue[T]:
        now = time.time()
        bucket_id = int(now // self.bucket) if self.bucket else 0
        cache_key = f"{key}:{bucket_id}"

        if self.bucket:
            hit = self._mem.get(key)
            if hit is not None and int(hit[0] // self.bucket) == bucket_id:
                return CacheValue(hit[1])

        retry_at = self._fail_until.get(cache_key, 0.0)
        if retry_at > now:
            raise CachedFailure(key, retry_at - now)

        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(self._fetch(key, cache_key, fetch))
            self._inflight[cache_key] = task

            def _drop(_task: asyncio.Task[CacheValue[T]]) -> None:
                self._inflight.pop(cache_key, None)

            task.add_done_callback(_drop)
        return await task

    async def _fetch(
        self,
        key: str,
        cache_key: str,
        fetch: Callable[[], Awaitable[T]],
    ) -> CacheValue[T]:
        try:
            value = await asyncio.wait_for(fetch(), timeout=self.timeout)
        except Exception:
            now = time.time()
            self._fail_until[cache_key] = now + self.negative
            hit = self._mem.get(key)
            if hit is not None and now - hit[0] <= self.stale:
                return CacheValue(hit[1], stale=True)
            raise

        now = time.time()
        self._mem[key] = (now, value)
        self._prune(now)
        return CacheValue(value)

    def _prune(self, now: float) -> None:
        if len(self._fail_until) > 512:
            self._fail_until = {
                key: until for key, until in self._fail_until.items() if until > now
            }
        if len(self._mem) > 512:
            keep_after = now - max(self.stale, self.bucket * 2)
            self._mem = {key: item for key, item in self._mem.items() if item[0] >= keep_after}

    def clear(self) -> None:
        self._mem.clear()
        self._inflight.clear()
        self._fail_until.clear()
