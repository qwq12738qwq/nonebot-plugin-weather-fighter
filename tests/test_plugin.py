from __future__ import annotations

import nonebot_plugin_weather_fighter as plugin


def test_plugin_metadata() -> None:
    assert plugin.__plugin_meta__.name == "天气战士"
    assert plugin.__plugin_meta__.type == "application"


def test_commands_registered() -> None:
    from nonebot_plugin_weather_fighter import matcher

    assert matcher.help_cmd is not None
    assert matcher.quote_cmd is not None
    assert matcher.weather_cmd is not None
    assert matcher.long_cmd is not None
    assert matcher.short_cmd is not None
    assert matcher.close_cmd is not None
    assert matcher.order_cmd is not None
    assert matcher.cancel_cmd is not None
