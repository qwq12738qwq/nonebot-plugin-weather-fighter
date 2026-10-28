from __future__ import annotations

import json
from importlib.resources import files

from nonebot_plugin_weather_fighter.services.provinces import (
    all_provinces,
    province_names,
    resolve_province,
)
from nonebot_plugin_weather_fighter.services.regions import region_cities


def test_all_provinces() -> None:
    provinces = all_provinces()
    assert len(provinces) == 34
    assert len({province.name for province in provinces}) == 34
    assert len({province.capital for province in provinces}) == 34
    for province in provinces:
        assert -90 <= province.coords[0] <= 90
        assert -180 <= province.coords[1] <= 180
        if province.station_coords is not None:
            assert -90 <= province.station_coords[0] <= 90
            assert -180 <= province.station_coords[1] <= 180


def test_resolve_province() -> None:
    assert resolve_province("河北") is not None
    assert resolve_province("河北").name == "河北省"  # type: ignore[union-attr]
    assert resolve_province("河北省").name == "河北省"  # type: ignore[union-attr]
    assert resolve_province("石家庄").name == "河北省"  # type: ignore[union-attr]
    assert resolve_province("hebei").name == "河北省"  # type: ignore[union-attr]
    assert resolve_province("北京").name == "北京市"  # type: ignore[union-attr]
    assert resolve_province("香港").name == "香港特别行政区"  # type: ignore[union-attr]
    assert resolve_province("新疆").name == "新疆维吾尔自治区"  # type: ignore[union-attr]
    assert resolve_province("不存在") is None
    assert resolve_province(None) is None
    assert resolve_province("  ") is None


def test_province_names() -> None:
    names = province_names()
    assert len(names) == 34
    assert "河北" in names
    assert "新疆" in names
    assert "台湾" in names


def test_region_selection() -> None:
    hebei = resolve_province("河北")
    assert hebei is not None
    selection = region_cities(hebei)
    assert len(selection.cities) == 8
    assert selection.cities[0].name == "石家庄"
    assert selection.scope == "河北"

    beijing = resolve_province("北京")
    assert beijing is not None
    nearby = region_cities(beijing)
    assert nearby.scope == "附近"
    assert len(nearby.cities) == 8
    assert nearby.cities[0].name == "北京"


def test_regions_reserved_dataset() -> None:
    resource = files("nonebot_plugin_weather_fighter").joinpath("resources/regions.json")
    document = json.loads(resource.read_text(encoding="utf-8"))
    assert len(document["provinces"]) == 34
    assert len(document["cities"]) == 352
    capitals = [city for city in document["cities"].values() if city["is_capital"]]
    assert len(capitals) == 34
    for city in document["cities"].values():
        assert city["coords"] is not None
        assert -90 <= city["coords"][0] <= 90
        assert -180 <= city["coords"][1] <= 180
