from __future__ import annotations

import asyncio
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
USER_AGENT = "nonebot-plugin-weather-fighter/0.2.0"


class HttpClient:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self.retries = max(1, retries)

    async def request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """带重试的请求；每次调用都从第 1 次开始，5xx 与传输错误才重试。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = await self._client.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
            if attempt < self.retries:
                await asyncio.sleep(min(2.0, 0.5 * attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("请求未执行")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.request(url, params=params, timeout=timeout)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


_http: HttpClient | None = None
_retries = DEFAULT_RETRIES


def configure_http(retries: int) -> None:
    global _retries
    _retries = max(1, int(retries))
    if _http is not None:
        _http.retries = _retries


def get_http() -> HttpClient:
    global _http
    if _http is None:
        _http = HttpClient(retries=_retries)
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None
