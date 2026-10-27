from __future__ import annotations

from datetime import datetime

import pytest

from nonebot_plugin_weather_fighter.services.market_time import TZ
from nonebot_plugin_weather_fighter.services.openmeteo import MinutelySeries
from nonebot_plugin_weather_fighter.services.provinces import resolve_province
from nonebot_plugin_weather_fighter.services.weather import (
    build_weather_report,
    format_weather_report,
)


def make_series() -> MinutelySeries:
    times = tuple(
        f"2026-10-27T{hour:02d}:{minute:02d}" for hour in range(9, 19) for minute in (0, 15, 30, 45)
    )
    size = len(times)
    return MinutelySeries(
        city_id="101090101",
        city_name="石家庄",
        time=times,
        temp=tuple(10.0 + index * 0.1 for index in range(size)),
        gust=tuple(3.0 for _ in range(size)),
        precip=tuple(0.0 for _ in range(size)),
        wcode=tuple(0 for _ in range(size)),
        cape=tuple(100.0 for _ in range(size)),
        dew=tuple(5.0 for _ in range(size)),
        fetched_at=1000.0,
        source="https://api.open-meteo.com/v1/forecast",
    )


def test_weather_report_works_after_close() -> None:
    province = resolve_province("石家庄")
    assert province is not None
    report = build_weather_report(
        make_series(),
        province,
        now=datetime(2026, 10, 27, 16, 0, tzinfo=TZ),
    )
    assert report.observed_at == "10-27 16:00"
    assert report.temp == pytest.approx(12.8)
    assert report.low == pytest.approx(10.0)
    assert report.high == pytest.approx(13.9)
    assert len(report.next_bars) == 4
    text = format_weather_report(report)
    assert "石家庄 天气" in text
    assert "现在 12.8℃" in text
    assert "今日 10.0 ~ 13.9℃" in text
    assert "未来 1 小时" in text
    assert "Open-Meteo" in text


def test_weather_report_rejects_empty_series() -> None:
    province = resolve_province("河北")
    assert province is not None
    empty = MinutelySeries(
        city_id=province.city_id,
        city_name=province.capital,
        time=(),
        temp=(),
        gust=(),
        precip=(),
        wcode=(),
        cape=(),
        dew=(),
    )
    with pytest.raises(ValueError):
        build_weather_report(empty, province)
