from __future__ import annotations

from datetime import date, datetime

from nonebot_plugin_weather_fighter.services.market_time import (
    TZ,
    add_trading_minutes,
    is_holiday,
    is_trading_day,
    market_status,
)


def test_statutory_holidays_are_closed() -> None:
    assert is_holiday(date(2026, 10, 1))
    assert not is_trading_day(date(2026, 10, 1))
    assert not is_trading_day(date(2026, 2, 17))
    assert not is_trading_day(date(2026, 9, 25))
    assert is_trading_day(date(2026, 9, 30))
    assert not is_trading_day(date(2026, 10, 10))


def test_holiday_market_status() -> None:
    moment = datetime(2026, 10, 1, 10, 0, tzinfo=TZ)
    text, opened = market_status(moment.timestamp())
    assert not opened
    assert "节假日" in text


def test_add_trading_minutes_skips_holidays() -> None:
    start = datetime(2026, 9, 30, 14, 45, tzinfo=TZ).timestamp()
    end = datetime.fromtimestamp(add_trading_minutes(start, 30), TZ)
    assert end.date() == date(2026, 10, 8)
    assert (end.hour, end.minute) == (9, 45)
