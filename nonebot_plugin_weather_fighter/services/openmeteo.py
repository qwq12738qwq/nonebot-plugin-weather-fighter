from __future__ import annotations

import asyncio
import json
import time as _time
from dataclasses import dataclass, field
from typing import Any

from .cache import BucketCache, CacheValue
from .coords import coords_query
from .http import HttpClient, get_http
from .market_time import trading_mask
from .provinces import Province
from .regions import RegionSelection

TIMEZONE = "Asia/Shanghai"
MINUTELY_FIELDS = (
    "temperature_2m",
    "wind_gusts_10m",
    "precipitation",
    "weather_code",
    "cape",
    "dew_point_2m",
)
FORECAST_HOSTS = (
    "https://historical-forecast-api.open-meteo.com/v1/forecast",
    "https://api.open-meteo.com/v1/forecast",
)


class OpenMeteoError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenMeteoConfig:
    bucket_seconds: int = 1800
    stale_seconds: int = 6 * 3600
    timeout: float = 30.0
    past_days: int = 92
    forecast_days: int = 1


@dataclass(frozen=True)
class MinutelySeries:
    city_id: str
    city_name: str
    time: tuple[str, ...]
    temp: tuple[float | None, ...]
    gust: tuple[float | None, ...]
    precip: tuple[float | None, ...]
    wcode: tuple[int | None, ...]
    cape: tuple[float | None, ...]
    dew: tuple[float | None, ...]
    fetched_at: float = field(default_factory=_time.time)
    source: str = ""

    def __len__(self) -> int:
        return len(self.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "city_id": self.city_id,
            "city_name": self.city_name,
            "time": list(self.time),
            "temp": list(self.temp),
            "gust": list(self.gust),
            "precip": list(self.precip),
            "wcode": list(self.wcode),
            "cape": list(self.cape),
            "dew": list(self.dew),
            "fetched_at": self.fetched_at,
            "source": self.source,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MinutelySeries:
        return cls(
            city_id=str(data["city_id"]),
            city_name=str(data.get("city_name") or ""),
            time=tuple(str(item) for item in data.get("time", ())),
            temp=tuple(_float_or_none(item) for item in data.get("temp", ())),
            gust=tuple(_float_or_none(item) for item in data.get("gust", ())),
            precip=tuple(_float_or_none(item) for item in data.get("precip", ())),
            wcode=tuple(_int_or_none(item) for item in data.get("wcode", ())),
            cape=tuple(_float_or_none(item) for item in data.get("cape", ())),
            dew=tuple(_float_or_none(item) for item in data.get("dew", ())),
            fetched_at=float(data.get("fetched_at") or _time.time()),
            source=str(data.get("source") or ""),
        )

    @classmethod
    def from_json(cls, payload: str) -> MinutelySeries:
        return cls.from_dict(json.loads(payload))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RegionSeries:
    province: str
    scope: str
    cities: tuple[str, ...]
    time: tuple[str, ...]
    temp: tuple[float | None, ...]
    gust: tuple[float | None, ...]
    precip: tuple[float | None, ...]
    wcode: tuple[float | None, ...]
    cape: tuple[float | None, ...]
    dew: tuple[float | None, ...]
    fetched_at: float = field(default_factory=_time.time)
    source: str = ""

    def __len__(self) -> int:
        return len(self.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "province": self.province,
            "scope": self.scope,
            "cities": list(self.cities),
            "time": list(self.time),
            "temp": list(self.temp),
            "gust": list(self.gust),
            "precip": list(self.precip),
            "wcode": list(self.wcode),
            "cape": list(self.cape),
            "dew": list(self.dew),
            "fetched_at": self.fetched_at,
            "source": self.source,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegionSeries:
        return cls(
            province=str(data.get("province") or ""),
            scope=str(data.get("scope") or ""),
            cities=tuple(str(item) for item in data.get("cities", ())),
            time=tuple(str(item) for item in data.get("time", ())),
            temp=tuple(_float_or_none(item) for item in data.get("temp", ())),
            gust=tuple(_float_or_none(item) for item in data.get("gust", ())),
            precip=tuple(_float_or_none(item) for item in data.get("precip", ())),
            wcode=tuple(_float_or_none(item) for item in data.get("wcode", ())),
            cape=tuple(_float_or_none(item) for item in data.get("cape", ())),
            dew=tuple(_float_or_none(item) for item in data.get("dew", ())),
            fetched_at=float(data.get("fetched_at") or _time.time()),
            source=str(data.get("source") or ""),
        )

    @classmethod
    def from_json(cls, payload: str) -> RegionSeries:
        return cls.from_dict(json.loads(payload))


def filter_trading(series: MinutelySeries) -> MinutelySeries:
    """只保留 A 股交易时段内的 15 分钟 bar（09:30-11:30、13:00-15:00）。"""
    indices = trading_mask(series.time)
    if len(indices) == len(series.time):
        return series

    def pick(values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(values[index] for index in indices)

    return MinutelySeries(
        city_id=series.city_id,
        city_name=series.city_name,
        time=pick(series.time),
        temp=pick(series.temp),
        gust=pick(series.gust),
        precip=pick(series.precip),
        wcode=pick(series.wcode),
        cape=pick(series.cape),
        dew=pick(series.dew),
        fetched_at=series.fetched_at,
        source=series.source,
    )


def filter_trading_region(series: RegionSeries) -> RegionSeries:
    indices = trading_mask(series.time)
    if len(indices) == len(series.time):
        return series

    def pick(values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(values[index] for index in indices)

    return RegionSeries(
        province=series.province,
        scope=series.scope,
        cities=series.cities,
        time=pick(series.time),
        temp=pick(series.temp),
        gust=pick(series.gust),
        precip=pick(series.precip),
        wcode=pick(series.wcode),
        cape=pick(series.cape),
        dew=pick(series.dew),
        fetched_at=series.fetched_at,
        source=series.source,
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _column(values: Any, size: int) -> list[Any]:
    column = list(values) if isinstance(values, (list, tuple)) else []
    if len(column) < size:
        column.extend([None] * (size - len(column)))
    return column[:size]


class OpenMeteoClient:
    def __init__(
        self,
        config: OpenMeteoConfig | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.config = config or OpenMeteoConfig()
        self._http = http or get_http()
        self._cache: BucketCache[MinutelySeries] = BucketCache(
            bucket=self.config.bucket_seconds,
            stale=self.config.stale_seconds,
            timeout=max(self.config.timeout, 30.0) * len(FORECAST_HOSTS) + 5,
        )
        self._region_cache: BucketCache[RegionSeries] = BucketCache(
            bucket=self.config.bucket_seconds,
            stale=self.config.stale_seconds,
            timeout=max(self.config.timeout, 30.0) * len(FORECAST_HOSTS) + 5,
        )

    async def fetch_minutely_15(self, province: Province) -> CacheValue[MinutelySeries]:
        key = f"minutely_15:{province.city_id}"
        return await self._cache.get(key, lambda: self._fetch_minutely_15(province))

    async def _fetch_minutely_15(self, province: Province) -> MinutelySeries:
        params = {
            **coords_query(province.coords),
            "minutely_15": ",".join(MINUTELY_FIELDS),
            "past_days": str(self.config.past_days),
            "forecast_days": str(self.config.forecast_days),
            "timezone": TIMEZONE,
        }
        errors: list[str] = []
        for host in FORECAST_HOSTS:
            try:
                payload = await asyncio.wait_for(
                    self._http.get_json(
                        host, params=params, timeout=max(self.config.timeout, 30.0)
                    ),
                    timeout=max(self.config.timeout, 30.0) + 3,
                )
                series = self._parse(province, payload, host)
                self._validate_history(series)
                return series
            except Exception as exc:  # noqa: BLE001 - 逐个主机兜底
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
        raise OpenMeteoError("Open-Meteo 15 分钟行情不可用；" + " | ".join(errors))

    async def fetch_region(
        self,
        province: Province,
        selection: RegionSelection,
        start_date: str,
        end_date: str,
    ) -> CacheValue[RegionSeries]:
        key = f"region:{province.city_id}:{start_date}:{end_date}"
        return await self._region_cache.get(
            key,
            lambda: self._fetch_region(province, selection, start_date, end_date),
        )

    async def _fetch_region(
        self,
        province: Province,
        selection: RegionSelection,
        start_date: str,
        end_date: str,
    ) -> RegionSeries:
        params = {
            "latitude": ",".join(f"{city.coords[0]:.4f}" for city in selection.cities),
            "longitude": ",".join(f"{city.coords[1]:.4f}" for city in selection.cities),
            "minutely_15": ",".join(MINUTELY_FIELDS),
            "start_date": start_date,
            "end_date": end_date,
            "timezone": TIMEZONE,
        }
        errors: list[str] = []
        for host in FORECAST_HOSTS:
            try:
                payload = await asyncio.wait_for(
                    self._http.get_json(
                        host, params=params, timeout=max(self.config.timeout, 30.0)
                    ),
                    timeout=max(self.config.timeout, 30.0) + 3,
                )
                series = self._parse_region(province, selection, payload, host)
                self._validate_gust(series.gust)
                return series
            except Exception as exc:  # noqa: BLE001 - 逐个主机兜底
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
        raise OpenMeteoError("Open-Meteo 区域行情不可用；" + " | ".join(errors))

    @staticmethod
    def _parse_region(
        province: Province,
        selection: RegionSelection,
        payload: Any,
        host: str,
    ) -> RegionSeries:
        blocks = payload if isinstance(payload, list) else [payload]
        blocks = [block for block in blocks if isinstance(block, dict)]
        if not blocks:
            raise OpenMeteoError("Open-Meteo 区域返回为空")
        first = None
        for block in blocks:
            candidate = block.get("minutely_15")
            if isinstance(candidate, dict) and isinstance(candidate.get("time"), list):
                if candidate["time"]:
                    first = candidate
                    break
        if first is None:
            raise OpenMeteoError("Open-Meteo 区域 minutely_15.time 为空")
        times = [str(item) for item in first["time"]]
        size = len(times)
        minutely = [
            block["minutely_15"] for block in blocks if isinstance(block.get("minutely_15"), dict)
        ]

        def average(field: str) -> tuple[float | None, ...]:
            out: list[float | None] = []
            for index in range(size):
                values: list[float] = []
                for block in minutely:
                    column = block.get(field)
                    if not isinstance(column, list) or index >= len(column):
                        continue
                    value = _float_or_none(column[index])
                    if value is not None:
                        values.append(value)
                out.append(sum(values) / len(values) if values else None)
            return tuple(out)

        return RegionSeries(
            province=province.name,
            scope=selection.scope,
            cities=tuple(city.name for city in selection.cities),
            time=tuple(times),
            temp=average("temperature_2m"),
            gust=average("wind_gusts_10m"),
            precip=average("precipitation"),
            wcode=average("weather_code"),
            cape=average("cape"),
            dew=average("dew_point_2m"),
            source=host,
        )

    @staticmethod
    def _parse(province: Province, payload: Any, host: str) -> MinutelySeries:
        if not isinstance(payload, dict):
            raise OpenMeteoError("Open-Meteo 返回非对象")
        block = payload.get("minutely_15")
        if not isinstance(block, dict):
            raise OpenMeteoError("Open-Meteo 返回缺少 minutely_15")
        times = block.get("time")
        if not isinstance(times, list) or not times:
            raise OpenMeteoError("Open-Meteo minutely_15.time 为空")
        size = len(times)
        return MinutelySeries(
            city_id=province.city_id,
            city_name=province.capital,
            time=tuple(str(item) for item in times),
            temp=tuple(_float_or_none(v) for v in _column(block.get("temperature_2m"), size)),
            gust=tuple(_float_or_none(v) for v in _column(block.get("wind_gusts_10m"), size)),
            precip=tuple(_float_or_none(v) for v in _column(block.get("precipitation"), size)),
            wcode=tuple(_int_or_none(v) for v in _column(block.get("weather_code"), size)),
            cape=tuple(_float_or_none(v) for v in _column(block.get("cape"), size)),
            dew=tuple(_float_or_none(v) for v in _column(block.get("dew_point_2m"), size)),
            source=host,
        )

    @staticmethod
    def _validate_gust(gust: tuple[float | None, ...]) -> None:
        probe = min(96, len(gust))
        if probe == 0:
            raise OpenMeteoError("行情序列为空")
        non_null = sum(1 for value in gust[:probe] if value is not None)
        if non_null < probe * 0.5:
            raise OpenMeteoError("这台主机的 15 分钟历史不够长")

    @classmethod
    def _validate_history(cls, series: MinutelySeries) -> None:
        cls._validate_gust(series.gust)


_client: OpenMeteoClient | None = None


def get_client() -> OpenMeteoClient:
    global _client
    if _client is None:
        _client = OpenMeteoClient()
    return _client


def configure_client(config: OpenMeteoConfig) -> OpenMeteoClient:
    global _client
    _client = OpenMeteoClient(config)
    return _client
