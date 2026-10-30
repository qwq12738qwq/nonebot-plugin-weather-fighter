from __future__ import annotations

import asyncio
import email.utils
import time
from datetime import timezone

from nonebot import logger

from .http import HttpClient, get_http

# 与行情数据同一来源：Date 响应头精确到秒，足够校准"当前 bar"的判定。
SYNC_URL = "https://api.open-meteo.com/v1/forecast"
SYNC_PARAMS = {"latitude": "0", "longitude": "0", "forecast_days": "1"}
# 默认值仅兜底；运行时用现有的 WEATHER_FIGHTER_HTTP_TIMEOUT 覆盖，不新增环境变量
SYNC_TIMEOUT = 8.0
SYNC_INTERVAL = 3600.0
SYNC_RETRY = 300.0
WARN_SKEW = 5.0


class SyncedClock:
    """以服务器 Date 头为准的市场时钟；主机时间只作单调基准，不直接参与判定。"""

    def __init__(
        self,
        *,
        url: str = SYNC_URL,
        interval: float = SYNC_INTERVAL,
        timeout: float = SYNC_TIMEOUT,
        http: HttpClient | None = None,
    ) -> None:
        self.url = url
        self.interval = interval
        self.timeout = timeout
        self._http = http
        self.offset = 0.0
        self.skew = 0.0
        self.synced = False
        self._failed = False
        self._lock = asyncio.Lock()

    def now(self) -> float:
        """当前服务器时间（Unix 秒）；尚未同步成功时退回主机时间。"""
        if not self.synced:
            return time.time()
        return time.monotonic() + self.offset

    async def sync(self) -> float | None:
        async with self._lock:
            started = time.monotonic()
            # 复用 HttpClient：按 WEATHER_FIGHTER_RETRY_COUNT 重试，每次同步都重置重试预算
            http = self._http or get_http()
            try:
                response = await http.request(self.url, params=SYNC_PARAMS, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - 同步失败不能影响主流程
                self._warn(exc)
                return None
            raw = response.headers.get("date")
            if not raw:
                self._warn("响应缺少 Date 头")
                return None
            try:
                moment = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError) as exc:
                self._warn(exc)
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            server = moment.timestamp()
            midpoint = (started + time.monotonic()) / 2
            self.offset = server - midpoint
            self.skew = time.monotonic() + self.offset - time.time()
            first = not self.synced
            self.synced = True
            if first:
                level = logger.warning if abs(self.skew) > WARN_SKEW else logger.info
                level(f"市场时钟已同步到服务器时间（主机偏差 {self.skew:+.2f}s）")
            elif abs(self.skew) > WARN_SKEW:
                logger.warning(f"市场时钟主机偏差 {self.skew:+.2f}s，已按服务器校准")
            return self.offset

    def _warn(self, exc: object) -> None:
        if not self.synced and not self._failed:
            self._failed = True
            logger.error(f"市场时钟未能与服务器同步，暂用主机时间：{exc}")
        else:
            logger.warning(f"市场时钟同步失败，沿用上次校准（偏差 {self.skew:+.2f}s）：{exc}")

    async def refresh_loop(self) -> None:
        while True:
            ok = await self.sync() is not None
            await asyncio.sleep(self.interval if ok else SYNC_RETRY)


_clock: SyncedClock | None = None


def get_clock() -> SyncedClock:
    global _clock
    if _clock is None:
        _clock = SyncedClock()
    return _clock


def configure_clock(timeout: float) -> SyncedClock:
    """沿用 WEATHER_FIGHTER_HTTP_TIMEOUT，不为时钟单开环境变量。"""
    clock = get_clock()
    clock.timeout = max(1.0, float(timeout))
    return clock
