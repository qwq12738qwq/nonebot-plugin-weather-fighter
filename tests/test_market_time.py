from __future__ import annotations

from datetime import datetime

from nonebot_plugin_weather_fighter.services.market_time import (
    BARS_PER_DAY,
    TZ,
    bar_of_day,
    elapsed_bars,
    market_status,
)


def at(month: int, day: int, hour: int, minute: int) -> float:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp()


def test_market_runs_all_day() -> None:
    assert BARS_PER_DAY == 96


def test_bar_of_day_covers_whole_day() -> None:
    assert bar_of_day(at(10, 27, 0, 0)) == 0
    assert bar_of_day(at(10, 27, 9, 30)) == 38
    assert bar_of_day(at(10, 27, 23, 59)) == 95


def test_elapsed_bars_counts_started_bars() -> None:
    start = at(10, 27, 10, 0)
    assert elapsed_bars(start, at(10, 27, 10, 30)) == 2
    assert elapsed_bars(start, start) == 0
    assert elapsed_bars(start, at(10, 27, 9, 0)) == 0
    # 跨夜也连续
    assert elapsed_bars(at(10, 27, 23, 45), at(10, 28, 0, 30)) == 3
    # 周末不再例外
    assert elapsed_bars(at(10, 24, 23, 45), at(10, 25, 0, 15)) == 2


def test_market_status_is_always_open() -> None:
    for hour in (3, 9, 12, 15, 23):
        text, opened = market_status(at(10, 31, hour, 0))  # 周六
        assert opened is True
        assert text == "交易中"
