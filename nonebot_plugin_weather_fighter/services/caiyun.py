from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http import HttpClient, get_http
from .provinces import Province

CAIYUN_WEATHER_URL = "https://api.caiyunapp.com/v2.6/{token}/{lon:.4f},{lat:.4f}/weather"


class CaiyunNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class CaiyunConfig:
    token: str = ""
    timeout: float = 10.0


async def fetch_realtime(
    province: Province,
    config: CaiyunConfig,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    """彩云 v2.6 实况（M1 预留；未配置 token 时不发起请求）。"""
    if not config.token:
        raise CaiyunNotConfigured("未配置 WEATHER_FIGHTER_CAIYUN_TOKEN")
    lat, lon = province.coords
    url = CAIYUN_WEATHER_URL.format(token=config.token, lat=lat, lon=lon)
    payload = await (http or get_http()).get_json(
        url,
        params={"alert": "true", "dailysteps": "1", "hourlysteps": "24", "minutelysteps": "48"},
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    realtime = result.get("realtime") if isinstance(result, dict) else None
    if not isinstance(realtime, dict):
        raise RuntimeError("彩云返回缺少 result.realtime")
    return realtime
