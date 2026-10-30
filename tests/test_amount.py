from __future__ import annotations

import pytest

from nonebot_plugin_weather_fighter.game.amount import AmountError, parse_amount


def test_parse_amount_units_and_percent() -> None:
    assert parse_amount("1000", 100_000) == pytest.approx(1000)
    assert parse_amount("1.5万", 100_000) == pytest.approx(15_000)
    assert parse_amount("2w", 100_000) == pytest.approx(20_000)
    assert parse_amount("5千", 100_000) == pytest.approx(5000)
    assert parse_amount("30%", 100_000) == pytest.approx(30_000)
    assert parse_amount("全", 100_000) == pytest.approx(100_000)
    assert parse_amount("", 100_000) == pytest.approx(20_000)
    assert parse_amount("1,000元", 100_000) == pytest.approx(1000)


def test_parse_amount_rejects_bad_values() -> None:
    with pytest.raises(AmountError):
        parse_amount("200000", 100_000)
    with pytest.raises(AmountError):
        parse_amount("abc", 100_000)
    with pytest.raises(AmountError):
        parse_amount("0", 100_000)
    with pytest.raises(AmountError):
        parse_amount("nan", 100_000)
    with pytest.raises(AmountError):
        parse_amount("inf", 100_000)
