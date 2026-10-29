from __future__ import annotations

import asyncio
import email.utils
import time

import httpx
import pytest
import respx

from nonebot_plugin_weather_fighter.services import clock as clock_mod
from nonebot_plugin_weather_fighter.services import http as http_mod
from nonebot_plugin_weather_fighter.services.clock import SyncedClock
from nonebot_plugin_weather_fighter.services.http import HttpClient

URL = "https://clock.test/time"


def server_date(epoch: float) -> dict[str, str]:
    return {"date": email.utils.formatdate(epoch, usegmt=True)}


@pytest.fixture
async def http_factory():
    clients: list[HttpClient] = []

    async def make(retries: int = 1) -> HttpClient:
        client = HttpClient(retries=retries)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", no_sleep)


@respx.mock
async def test_sync_uses_server_date_header(http_factory) -> None:
    server = time.time() + 3600
    respx.get(URL).mock(return_value=httpx.Response(200, headers=server_date(server)))
    clock = SyncedClock(url=URL, http=await http_factory())
    assert clock.synced is False
    offset = await clock.sync()
    assert offset is not None
    assert clock.synced is True
    assert clock.skew > 3000  # 主机比服务器慢约一小时
    assert abs(clock.now() - server) <= 2


@respx.mock
async def test_synced_clock_ignores_host_wall_clock(
    http_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = time.time()
    respx.get(URL).mock(return_value=httpx.Response(200, headers=server_date(server)))
    clock = SyncedClock(url=URL, http=await http_factory())
    await clock.sync()
    before = clock.now()
    # 主机墙钟被拨快一天，同步后的市场时间不受影响
    monkeypatch.setattr(clock_mod.time, "time", lambda: before + 86400)
    assert abs(clock.now() - before) < 1


@respx.mock
async def test_missing_date_header_falls_back_to_host_time(http_factory) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200))
    clock = SyncedClock(url=URL, http=await http_factory())
    assert await clock.sync() is None
    assert clock.synced is False
    assert abs(clock.now() - time.time()) < 2


@respx.mock
async def test_network_error_falls_back_to_host_time(http_factory) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    clock = SyncedClock(url=URL, http=await http_factory())
    assert await clock.sync() is None
    assert clock.synced is False
    assert abs(clock.now() - time.time()) < 2


@respx.mock
async def test_sync_retries_follow_http_client_budget(http_factory) -> None:
    server = time.time()
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, headers=server_date(server))]
    )
    clock = SyncedClock(url=URL, http=await http_factory(retries=2))
    assert await clock.sync() is not None
    assert route.call_count == 2


@respx.mock
async def test_sync_resets_retry_budget_each_call(http_factory) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    clock = SyncedClock(url=URL, http=await http_factory(retries=2))
    assert await clock.sync() is None
    assert route.call_count == 2
    assert await clock.sync() is None
    assert route.call_count == 4  # 第二次同步重新从第 1 次尝试开始


@respx.mock
async def test_failed_resync_keeps_previous_calibration(http_factory) -> None:
    server = time.time() + 120
    route = respx.get(URL).mock(return_value=httpx.Response(200, headers=server_date(server)))
    clock = SyncedClock(url=URL, http=await http_factory())
    await clock.sync()
    first = clock.now()
    route.mock(side_effect=httpx.ConnectError("boom"))
    assert await clock.sync() is None
    assert clock.synced is True
    assert abs(clock.now() - first) < 2


async def test_refresh_loop_syncs_then_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SyncedClock(url=URL)
    calls: list[int] = []

    async def fake_sync() -> float:
        calls.append(1)
        return 0.0

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(clock, "sync", fake_sync)
    monkeypatch.setattr(clock_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await clock.refresh_loop()
    assert calls == [1]


class FakeHttp:
    def __init__(self, server: float) -> None:
        self.server = server
        self.calls: list[float | None] = []

    async def request(self, url: str, *, params=None, timeout=None):
        self.calls.append(timeout)
        return httpx.Response(200, headers=server_date(self.server))


async def test_sync_uses_configured_timeout() -> None:
    fake = FakeHttp(time.time())
    clock = SyncedClock(url=URL, timeout=3.5, http=fake)  # type: ignore[arg-type]
    assert await clock.sync() is not None
    assert fake.calls == [3.5]


def test_configure_clock_reuses_existing_http_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock_mod, "_clock", None)
    clock = clock_mod.configure_clock(12.5)
    assert clock.timeout == 12.5
    assert clock_mod.get_clock() is clock
