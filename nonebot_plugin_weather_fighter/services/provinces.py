from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any


@dataclass(frozen=True, slots=True)
class Province:
    name: str
    short: str
    capital: str
    city_id: str
    cma_station: str
    coords: tuple[float, float]
    station_coords: tuple[float, float] | None
    pinyin: str
    aliases: tuple[str, ...]


def _coord(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lat, lon = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _load_document() -> dict[str, Any]:
    resource = files("nonebot_plugin_weather_fighter").joinpath("resources/provinces.json")
    data: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def all_provinces() -> tuple[Province, ...]:
    document = _load_document()
    out: list[Province] = []
    for item in document["provinces"]:
        coords = _coord(item.get("coords"))
        if coords is None:
            raise ValueError(f"省份坐标无效: {item.get('name')}")
        station = _coord(item.get("station_coords"))
        aliases = tuple(str(alias) for alias in item.get("aliases", ()) if str(alias).strip())
        out.append(
            Province(
                name=str(item["name"]),
                short=str(item["short"]),
                capital=str(item["capital"]),
                city_id=str(item["city_id"]),
                cma_station=str(item.get("cma_station") or ""),
                coords=coords,
                station_coords=station,
                pinyin=str(item.get("pinyin") or ""),
                aliases=aliases,
            )
        )
    return tuple(out)


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, Province]:
    index: dict[str, Province] = {}
    for province in all_provinces():
        keys = {province.name, province.short, province.capital, province.pinyin}
        keys.update(province.aliases)
        for key in keys:
            normalized = "".join(key.split()).casefold()
            if normalized:
                index.setdefault(normalized, province)
    return index


def resolve_province(text: str | None) -> Province | None:
    if not text:
        return None
    key = "".join(text.split()).casefold()
    if not key:
        return None
    return _alias_index().get(key)


def province_names() -> list[str]:
    return [province.short for province in all_provinces()]
