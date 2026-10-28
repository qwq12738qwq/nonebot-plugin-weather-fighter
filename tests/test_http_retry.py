from __future__ import annotations

import httpx
import pytest

from nonebot_plugin_weather_fighter.services import http as http_module
from nonebot_plugin_weather_fighter.services.http import HttpClient


def _client(handler: object, *, retries: int) -> HttpClient:
    client = HttpClient(retries=retries)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return client


@pytest.mark.asyncio
async def test_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(http_module.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = _client(handler, retries=3)
    assert await client.get_json("https://example.invalid") == {"ok": True}
    assert calls["n"] == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(http_module.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, request=request)

    client = _client(handler, retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_json("https://example.invalid")
    assert calls["n"] == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(http_module.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, request=request)

    client = _client(handler, retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_json("https://example.invalid")
    assert calls["n"] == 1
    await client.aclose()
