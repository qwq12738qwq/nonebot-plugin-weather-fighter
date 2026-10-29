from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from nonebot_plugin_weather_fighter.game.wxi import (
    BARS_PER_DAY,
    TRADE_DAYS,
    TZ,
    WARM_DAYS,
    arrays_from_minutely,
    build_wxi_series,
    comfort_values,
    forecast_series,
    pick_window_start,
)
from nonebot_plugin_weather_fighter.services.openmeteo import (
    ForecastDeviation,
    MinutelySeries,
    RegionSeries,
)


def make_local(points: int = 8928) -> MinutelySeries:
    end = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    start = end - timedelta(minutes=15 * (points - 1))
    times = tuple(
        (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M") for i in range(points)
    )
    temp = tuple(20 + 8 * math.sin(i / 96) + 2 * math.sin(i / 7) for i in range(points))
    gust = tuple(5 + 3 * abs(math.sin(i / 48)) + (8 if i % 400 == 0 else 0) for i in range(points))
    precip = tuple(2.0 if i % 500 == 0 else 0.0 for i in range(points))
    wcode = tuple(95 if i % 500 == 0 else (61 if i % 250 == 0 else 2) for i in range(points))
    cape = tuple(2500 if i % 500 == 0 else 200 + 100 * abs(math.sin(i / 30)) for i in range(points))
    dew = tuple(10 + 5 * math.sin(i / 96) for i in range(points))
    return MinutelySeries(
        city_id="101090101",
        city_name="石家庄",
        time=times,
        temp=temp,
        gust=gust,
        precip=precip,
        wcode=wcode,
        cape=cape,
        dew=dew,
        fetched_at=1000.0,
        source="test",
    )


def make_region(local: MinutelySeries) -> RegionSeries:
    return RegionSeries(
        province="河北省",
        scope="河北",
        cities=("石家庄", "保定", "唐山"),
        time=local.time,
        temp=tuple(None if value is None else value + 0.8 for value in local.temp),
        gust=tuple(None if value is None else value + 0.5 for value in local.gust),
        precip=tuple(None if value is None else value * 0.8 for value in local.precip),
        wcode=tuple(None if value is None else float(value) for value in local.wcode),
        cape=tuple(None if value is None else value * 1.1 for value in local.cape),
        dew=tuple(None if value is None else value + 0.5 for value in local.dew),
        fetched_at=1000.0,
        source="test",
    )


def test_wxi_series_shape_and_regional_line() -> None:
    raw = make_local()
    local = raw
    region = make_region(raw)
    seed = 123456789
    window_start = pick_window_start(local.time, seed)
    wxi = build_wxi_series(
        local,
        region,
        province_name="河北省",
        province_short="河北",
        city_name="石家庄",
        city_id=local.city_id,
        seed=seed,
        window_start=window_start,
    )
    assert len(wxi) == (WARM_DAYS + TRADE_DAYS) * BARS_PER_DAY
    assert wxi.warm_bars == WARM_DAYS * BARS_PER_DAY
    assert len(wxi.regional_line) == len(wxi)
    assert all(value is not None for value in wxi.regional_line)
    for bar in wxi.bars:
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert 0 < bar.comfort <= 100
        assert bar.activity > 0
        assert 500 < bar.close < 2000
        # 区域线就是区域均温本身（测试数据 = 本地气温 + 0.8），不再是合成指数
        assert bar.regional is not None
        assert 5 < bar.regional < 45
        assert bar.regional == pytest.approx(bar.temp + 0.8)


def test_forecast_deviation_enters_wxi() -> None:
    raw = make_local()
    local = raw
    region = make_region(raw)
    seed = 123456789
    window_start = pick_window_start(local.time, seed)
    deviation = tuple(1.8 * math.sin(index / 17) for index in range(len(local.time)))
    actual = tuple(value + offset for value, offset in zip(local.temp, deviation, strict=True))
    forecast = ForecastDeviation(
        city_id=local.city_id,
        city_name=local.city_name,
        time=local.time,
        actual=actual,
        forecast=local.temp,
        source="test",
    )
    raw_dev, normalized = forecast_series(local.time, forecast)
    assert normalized is not None
    assert any(value is not None for value in raw_dev)

    kwargs = {
        "province_name": "河北省",
        "province_short": "河北",
        "city_name": "石家庄",
        "city_id": local.city_id,
        "seed": seed,
        "window_start": window_start,
    }
    with_forecast = build_wxi_series(local, region, forecast=forecast, **kwargs)
    without = build_wxi_series(local, region, **kwargs)
    assert with_forecast.bars[0].forecast_dev is not None
    assert [bar.close for bar in with_forecast.bars] != [bar.close for bar in without.bars]


def test_wxi_is_deterministic() -> None:
    raw = make_local()
    local = raw
    region = make_region(raw)
    kwargs = dict(
        province_name="河北省",
        province_short="河北",
        city_name="石家庄",
        city_id=local.city_id,
        seed=42,
        window_start=0,
    )
    first = build_wxi_series(local, region, **kwargs)
    second = build_wxi_series(local, region, **kwargs)
    assert first.bars == second.bars
    other = build_wxi_series(local, region, **{**kwargs, "seed": 43})
    assert first.bars != other.bars


def test_comfort_values_are_bounded() -> None:
    local = make_local(1000)
    values = comfort_values(arrays_from_minutely(local))
    assert len(values) == 1000
    assert min(values) > 0
    assert max(values) <= 100
