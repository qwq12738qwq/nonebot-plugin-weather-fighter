from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, Message, PrivateMessageEvent
from nonebug import App

from nonebot_plugin_weather_fighter.game.engine import GameEngine
from nonebot_plugin_weather_fighter.services.store import Store

from .test_engine import FakeClient


def private_event(text: str) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=10000,
        post_type="message",
        sub_type="friend",
        user_id=123456,
        message_type="private",
        message_id=1,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender={"user_id": 123456, "nickname": "tester"},
        to_me=True,
    )


async def make_engine(tmp_path: Path) -> GameEngine:
    store = Store(tmp_path / "game.db")
    await store.initialize()
    return GameEngine(store, FakeClient(), chart_bars=120)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_help_command(app: App) -> None:
    from nonebot_plugin_weather_fighter.matcher import help_cmd

    async with app.test_matcher(help_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/帮助")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(help_cmd)


@pytest.mark.asyncio
async def test_quote_auto_starts_and_sends_chart(
    app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)

    async def fake_render(*args, **kwargs) -> bytes:
        return b"PNG"

    monkeypatch.setattr(matcher, "get_engine", lambda: engine)
    monkeypatch.setattr(
        "nonebot_plugin_weather_fighter.render.renderer.render_main_chart",
        fake_render,
    )

    async with app.test_matcher(matcher.quote_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/行情 河北")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.quote_cmd)
    await engine.store.close()


@pytest.mark.asyncio
async def test_bet_auto_starts(app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)
    monkeypatch.setattr(matcher, "get_engine", lambda: engine)

    async with app.test_matcher(matcher.long_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/买入 河北 1000")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.long_cmd)
    await engine.store.close()


@pytest.mark.asyncio
async def test_sell_auto_starts(app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)
    monkeypatch.setattr(matcher, "get_engine", lambda: engine)

    async with app.test_matcher(matcher.short_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/卖出 河北 1000")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.short_cmd)
    await engine.store.close()


@pytest.mark.asyncio
async def test_order_command_uses_temperature_price(
    app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)
    monkeypatch.setattr(matcher, "get_engine", lambda: engine)

    async with app.test_matcher(matcher.order_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/到价 河北 16.5 买入 1000")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.order_cmd)
    await engine.store.close()


@pytest.mark.asyncio
async def test_close_without_board_is_noop(
    app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)
    monkeypatch.setattr(matcher, "get_engine", lambda: engine)

    async with app.test_matcher(matcher.close_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/平仓 河北")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.close_cmd)
    await engine.store.close()


@pytest.mark.asyncio
async def test_cancel_without_board(
    app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nonebot_plugin_weather_fighter import matcher

    engine = await make_engine(tmp_path)
    monkeypatch.setattr(matcher, "get_engine", lambda: engine)

    async with app.test_matcher(matcher.cancel_cmd) as ctx:
        adapter = ctx.create_adapter(base=Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        event = private_event("/撤单")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, ANY, bot=bot)
        ctx.should_finished(matcher.cancel_cmd)
    await engine.store.close()
