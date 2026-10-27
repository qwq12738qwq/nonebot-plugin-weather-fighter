from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "nonebot-plugin-weather-fighter/0.2.0"


class HttpClient:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self._client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


_http: HttpClient | None = None


def get_http() -> HttpClient:
    global _http
    if _http is None:
        _http = HttpClient()
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None
