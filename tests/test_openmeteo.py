from __future__ import annotations

import httpx
import pytest
import respx

from nonebot_plugin_weather_fighter.services.openmeteo import (
    FORECAST_HOSTS,
    OpenMeteoClient,
    OpenMeteoConfig,
    OpenMeteoError,
)
from nonebot_plugin_weather_fighter.services.provinces import resolve_province
from nonebot_plugin_weather_fighter.services.regions import region_cities


def payload(
    *, gust: float = 5.0, temp: float = 20.0, wcode: int = 0, points: int = 192
) -> dict[str, object]:
    times = [f"2026-08-01T{(i // 60) % 24:02d}:{i % 60:02d}" for i in range(points)]
    return {
        "minutely_15": {
            "time": times,
            "temperature_2m": [temp] * points,
            "wind_gusts_10m": [gust] * points,
            "precipitation": [0.0] * points,
            "weather_code": [wcode] * points,
            "cape": [100.0] * points,
            "dew_point_2m": [10.0] * points,
        }
    }


def client() -> OpenMeteoClient:
    return OpenMeteoClient(OpenMeteoConfig(timeout=1, bucket_seconds=1800))


def province():
    found = resolve_province("河北")
    assert found is not None
    return found


@respx.mock
async def test_fetch_and_cache() -> None:
    route = respx.get(FORECAST_HOSTS[0]).mock(return_value=httpx.Response(200, json=payload()))
    subject = client()
    result = await subject.fetch_minutely_15(province())
    assert result.stale is False
    assert len(result.value) == 192
    assert result.value.city_id == "101090101"
    assert result.value.source == FORECAST_HOSTS[0]
    assert route.calls[0].request.url.params.get("wind_speed_unit") == "ms"

    again = await subject.fetch_minutely_15(province())
    assert again.value.time == result.value.time
    assert route.call_count == 1


@respx.mock
async def test_fallback_when_history_is_short() -> None:
    respx.get(FORECAST_HOSTS[0]).mock(
        return_value=httpx.Response(200, json=payload(gust=None))  # type: ignore[arg-type]
    )
    second = respx.get(FORECAST_HOSTS[1]).mock(return_value=httpx.Response(200, json=payload()))
    result = await client().fetch_minutely_15(province())
    assert result.value.source == FORECAST_HOSTS[1]
    assert second.called


@respx.mock
async def test_all_hosts_fail() -> None:
    for host in FORECAST_HOSTS:
        respx.get(host).mock(return_value=httpx.Response(500))
    with pytest.raises(OpenMeteoError):
        await client().fetch_minutely_15(province())


@respx.mock
async def test_region_average() -> None:
    route = respx.get(FORECAST_HOSTS[0]).mock(
        return_value=httpx.Response(200, json=[payload(), payload()])
    )
    found = province()
    selection = region_cities(found)
    result = await client().fetch_region(found, selection, "2026-08-01", "2026-08-02")
    assert result.value.temp[0] == 20.0
    assert result.value.cities == tuple(city.name for city in selection.cities)
    assert result.value.scope == selection.scope
    assert route.called


@respx.mock
async def test_region_is_weighted_by_city_grade() -> None:
    respx.get(FORECAST_HOSTS[0]).mock(
        return_value=httpx.Response(
            200,
            json=[
                payload(temp=20.0, wcode=61),
                payload(temp=30.0, wcode=61),
                payload(temp=30.0, wcode=95),
            ],
        )
    )
    found = province()
    selection = region_cities(found)
    weights = [city.weight for city in selection.cities[:3]]
    result = await client().fetch_region(found, selection, "2026-08-01", "2026-08-02")
    expected = (20.0 * weights[0] + 30.0 * weights[1] + 30.0 * weights[2]) / sum(weights)
    assert result.value.temp[0] == pytest.approx(expected)
    assert result.value.temp[0] != 26.666666  # 不是简单平均
    assert result.value.wcode[0] in (61.0, 95.0)  # 众数而不是线性平均


@respx.mock
async def test_region_code_ties_pick_worst_weather() -> None:
    respx.get(FORECAST_HOSTS[0]).mock(
        return_value=httpx.Response(200, json=[payload(wcode=2), payload(wcode=3)])
    )
    found = province()
    selection = region_cities(found)
    first, second = selection.cities[0].weight, selection.cities[1].weight
    result = await client().fetch_region(found, selection, "2026-08-01", "2026-08-02")
    assert result.value.wcode[0] == (3.0 if first == second else 2.0)


@respx.mock
async def test_fallback_when_temperature_history_is_missing() -> None:
    broken = payload()
    broken["minutely_15"]["temperature_2m"] = [None] * 192
    respx.get(FORECAST_HOSTS[0]).mock(return_value=httpx.Response(200, json=broken))
    second = respx.get(FORECAST_HOSTS[1]).mock(return_value=httpx.Response(200, json=payload()))
    result = await client().fetch_minutely_15(province())
    assert result.value.source == FORECAST_HOSTS[1]
    assert second.called


@respx.mock
async def test_empty_payload_fails() -> None:
    for host in FORECAST_HOSTS:
        respx.get(host).mock(return_value=httpx.Response(200, json={"minutely_15": {"time": []}}))
    with pytest.raises(OpenMeteoError):
        await client().fetch_minutely_15(province())
