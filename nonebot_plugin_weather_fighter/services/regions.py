from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .provinces import Province, resolve_province

# 直辖市＝省会本身就是整个省级单位（没有同城其他站点可交叉验证），故优先于省会 2.2；香港澳门按省会档。
MUNICIPALITIES = {"北京市", "上海市", "天津市", "重庆市"}


@dataclass(frozen=True, slots=True)
class City:
    city_id: str
    name: str
    province: str
    pinyin: str
    coords: tuple[float, float]
    station_coords: tuple[float, float] | None
    cma_station: str
    is_capital: bool

    @property
    def weight(self) -> float:
        if self.province in MUNICIPALITIES:
            return 3.0
        if self.is_capital:
            return 2.2
        if self.cma_station:
            return 1.6
        return 0.6


def dish_scale(weight: float) -> float:
    return max(0.35, min(2.6, 1.6 / weight))


def city_amp(weight: float) -> float:
    return 0.8389 * (dish_scale(weight) ** 0.19)


@dataclass(frozen=True, slots=True)
class RegionSelection:
    cities: tuple[City, ...]
    scope: str


def _coords(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lat, lon = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


@lru_cache(maxsize=1)
def all_cities() -> tuple[City, ...]:
    resource = files("nonebot_plugin_weather_fighter").joinpath("resources/regions.json")
    document = json.loads(resource.read_text(encoding="utf-8"))
    out: list[City] = []
    for item in document["cities"].values():
        coords = _coords(item.get("coords"))
        if coords is None:
            continue
        out.append(
            City(
                city_id=str(item["city_id"]),
                name=str(item["name"]),
                province=str(item["province"]),
                pinyin=str(item.get("pinyin") or ""),
                coords=coords,
                station_coords=_coords(item.get("station_coords")),
                cma_station=str(item.get("cma_station") or ""),
                is_capital=bool(item.get("is_capital")),
            )
        )
    return tuple(out)


@lru_cache(maxsize=1)
def _name_index() -> dict[str, City]:
    index: dict[str, City] = {}
    for city in all_cities():
        for key in (city.name, city.pinyin):
            normalized = "".join(key.split()).casefold()
            if normalized:
                index.setdefault(normalized, city)
    return index


def resolve_city(text: str | None) -> City | None:
    if not text:
        return None
    key = "".join(text.split()).casefold()
    if not key:
        return None
    return _name_index().get(key)


def resolve_province_or_city(text: str | None) -> Province | None:
    """省份优先；城市/地区名暂时归到所属省级。"""
    province = resolve_province(text)
    if province is not None:
        return province
    city = resolve_city(text)
    if city is None:
        return None
    return resolve_province(city.province)


@lru_cache(maxsize=1)
def cities_by_province() -> dict[str, tuple[City, ...]]:
    grouped: dict[str, list[City]] = {}
    for city in all_cities():
        grouped.setdefault(city.province, []).append(city)
    return {
        province: tuple(sorted(cities, key=lambda item: (not item.is_capital, item.city_id)))
        for province, cities in grouped.items()
    }


def region_cities(province: Province, limit: int = 8) -> RegionSelection:
    """同省最多 8 城；直辖市/城市太少时退化为最近的几个城市。"""
    local = list(cities_by_province().get(province.name, ()))
    if len(local) < 4:
        lat, lon = province.coords
        nearby = sorted(
            (city for city in all_cities() if city.city_id != province.city_id),
            key=lambda city: (
                abs(city.coords[0] - lat) + abs(city.coords[1] - lon),
                city.city_id,
            ),
        )
        local = [city for city in local if city.city_id == province.city_id] + nearby
        scope = "附近"
    else:
        scope = province.short

    dedup: dict[str, City] = {}
    for city in local:
        dedup.setdefault(city.city_id, city)
    ordered = sorted(dedup.values(), key=lambda item: (not item.is_capital, item.city_id))
    return RegionSelection(tuple(ordered[:limit]), scope)
