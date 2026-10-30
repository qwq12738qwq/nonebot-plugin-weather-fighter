from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nonebot_plugin_weather_fighter.game.engine import (
    TOTAL_BARS,
    CloseOutcome,
    GameEngine,
    GameError,
    _bet_value,
    _wipe_price,
)
from nonebot_plugin_weather_fighter.game.wxi import WxiBar
from nonebot_plugin_weather_fighter.models import Account, OrderKind, Position, Side
from nonebot_plugin_weather_fighter.services.cache import CacheValue
from nonebot_plugin_weather_fighter.services.openmeteo import ForecastDeviation, MinutelySeries
from nonebot_plugin_weather_fighter.services.provinces import resolve_province
from nonebot_plugin_weather_fighter.services.store import Store

from .test_wxi import make_local, make_region

TZ = ZoneInfo("Asia/Shanghai")


def at(day: int, hour: int, minute: int) -> float:
    from datetime import datetime

    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


class FakeClient:
    def __init__(self) -> None:
        self.local = make_local()
        self.region = make_region(self.local)

    async def fetch_minutely_15(self, province) -> CacheValue[MinutelySeries]:
        return CacheValue(self.local)

    async def fetch_region(self, province, selection, start_date, end_date):
        return CacheValue(self.region)

    async def fetch_forecast_deviation(self, province):
        return CacheValue(
            ForecastDeviation(
                city_id=province.city_id,
                city_name=province.capital,
                time=(),
                actual=(),
                forecast=(),
            )
        )


async def make_engine(
    tmp_path: Path, now: dict[str, float]
) -> tuple[GameEngine, Store, FakeClient]:
    store = Store(tmp_path / "game.db")
    await store.initialize()
    client = FakeClient()
    engine = GameEngine(
        store,
        client,  # type: ignore[arg-type]
        chart_bars=120,
        clock=lambda: now["value"],
    )
    return engine, store, client


async def test_shared_board_and_independent_players(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None

    first = await engine.ensure_game("u1", "g1", hebei)
    second = await engine.ensure_game("u2", "g1", hebei)
    assert first.season.id == second.season.id
    assert first.session.id != second.session.id
    assert first.wxi.region_cities
    assert len(engine.chart_data(first).bars) == 120

    outcome = await engine.open_position("u1", "g1", hebei, Side.LONG, stake=1000)
    trade = outcome.trade
    assert trade.qty >= 1
    assert trade.stake <= 1000
    assert trade.margin == pytest.approx(trade.stake)
    assert len(outcome.state.positions) == 1
    assert outcome.state.positions[0].province == "河北省"
    assert outcome.state.positions[0].qty == trade.qty
    assert outcome.state.account.cash == pytest.approx(10_000 - trade.stake)

    other = await engine.ensure_game("u2", "g1", hebei)
    assert other.positions == ()
    assert other.account.cash == pytest.approx(10_000)
    await store.close()


async def test_leverage_defaults_and_cap(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None

    state = await engine.ensure_game("u1", "g1", hebei)
    assert state.session.leverage == pytest.approx(1.0)

    state = await engine.set_leverage("u1", "g1", 5)
    assert state.session.leverage == pytest.approx(5.0)
    with pytest.raises(GameError):
        await engine.set_leverage("u1", "g1", 101)

    await engine.open_position("u1", "g1", hebei, Side.LONG, stake=1000)
    with pytest.raises(GameError):
        await engine.set_leverage("u1", "g1", 2)
    await store.close()


async def test_board_opens_before_first_command(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None

    board = await engine.open_board(hebei)
    assert board.owner == "global"
    state = await engine.ensure_game("u1", "g1", None)
    assert state.season.id == board.id
    assert state.wxi.province_name == "河北省"
    await store.close()


async def test_board_is_shared_across_groups(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None

    first = await engine.ensure_game("u1", "g1", hebei)
    second = await engine.ensure_game("u2", "g2", hebei)
    assert first.season.id == second.season.id

    await engine.open_position("u1", "g1", hebei, Side.LONG, stake=1000)
    other_group = await engine.ensure_game("u1", "g2", hebei)
    same_group = await engine.ensure_game("u1", "g1", hebei)
    assert other_group.session.id != same_group.session.id
    assert other_group.account.cash == pytest.approx(10_000)
    assert other_group.positions == ()
    assert same_group.positions[0].province == "河北省"
    await store.close()


async def test_board_clock_advances_continuously(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)

    # 原午休时段也照常出下一根
    now["value"] = at(27, 12, 0)
    noon = await engine.quote("u1", "g1", hebei)
    assert noon.current.time != state.current.time
    assert engine.quote_stats(noon).market_status == "交易中"

    now["value"] = at(27, 12, 45)
    later = await engine.quote("u1", "g1", hebei)
    assert later.current.time.endswith("12:45")

    now["value"] = at(27, 13, 15)
    afternoon = await engine.quote("u1", "g1", hebei)
    assert afternoon.current.time.endswith("13:15")
    await store.close()


async def test_same_player_can_hold_multiple_provinces(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    shandong = resolve_province("山东")
    assert hebei is not None and shandong is not None

    hb = await engine.ensure_game("u1", "g1", hebei)
    sd = await engine.ensure_game("u1", "g1", shandong)
    assert hb.season.id == sd.season.id
    assert hb.session.id == sd.session.id
    assert hb.wxi.province_name == "河北省"
    assert sd.wxi.province_name == "山东省"

    hb_trade = await engine.open_position("u1", "g1", hebei, Side.LONG, stake=1000)
    both = await engine.open_position("u1", "g1", shandong, Side.SHORT, stake=2000)
    assert {position.province for position in both.state.positions} == {"河北省", "山东省"}
    spent = hb_trade.trade.stake + both.trade.stake
    assert both.state.total_stake == pytest.approx(spent)
    assert both.state.account.cash == pytest.approx(10_000 - spent)
    assert both.state.position_value > 0

    with pytest.raises(GameError):
        await engine.open_position("u1", "g1", hebei, Side.LONG, stake=1000)

    hb_closed = await engine.close_position("u1", "g1", hebei)
    assert len(hb_closed.trades) == 1
    assert {position.province for position in hb_closed.state.positions} == {"山东省"}
    assert hb_closed.state.total_stake == pytest.approx(both.trade.stake)

    all_closed = await engine.close_position("u1", "g1")
    assert all_closed.state.positions == ()
    assert len(all_closed.trades) == 1
    assert all_closed.state.account.cash == pytest.approx(10_000, rel=0.2)
    await store.close()


async def test_price_triggered_bet_and_close(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)

    target = state.current.close
    await engine.place_order("u1", "g1", hebei, OrderKind.BET, Side.LONG, target, stake=1000)

    now["value"] = at(27, 10, 15)
    filled = await engine.quote("u1", "g1", hebei)
    assert len(filled.positions) == 1
    assert filled.positions[0].entry_price == pytest.approx(target)
    assert filled.positions[0].province == "河北省"
    assert filled.orders == ()

    close_target = filled.current.close
    await engine.place_order("u1", "g1", hebei, OrderKind.CLOSE, None, close_target)

    now["value"] = at(27, 10, 30)
    closed = await engine.quote("u1", "g1", hebei)
    assert closed.positions == ()
    await store.close()


async def test_long_offline_backfills_order(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    target = state.current.close
    await engine.place_order("u1", "g1", hebei, OrderKind.BET, Side.LONG, target, stake=1000)

    from datetime import datetime

    now["value"] = datetime(2026, 11, 24, 10, 0, tzinfo=TZ).timestamp()
    back = await engine.quote("u1", "g1", hebei)
    assert len(back.positions) == 1
    assert back.positions[0].entry_price == pytest.approx(target)
    await store.close()


async def test_close_without_position_is_noop(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    first = await engine.ensure_game("u1", "g1", hebei)
    second = await engine.ensure_game("u2", "g1", hebei)

    outcome = await engine.close_position("u1", "g1")
    assert isinstance(outcome, CloseOutcome)
    assert outcome.trades == ()
    assert outcome.state is not None
    assert outcome.state.session.status.value == "running"
    assert outcome.state.season.id == second.season.id

    still_running = await engine.ensure_game("u2", "g1", hebei)
    assert still_running.session.status.value == "running"
    assert still_running.season.id == first.season.id
    await store.close()


async def test_fresh_group_member_is_open_by_default(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    await engine.ensure_game("u1", "g1", hebei)

    no_op = await engine.close_position("u2", "g1")
    assert isinstance(no_op, CloseOutcome)
    assert no_op.trades == ()
    assert no_op.state is not None
    assert no_op.state.session.status.value == "running"

    opened = await engine.open_position("u2", "g1", hebei, Side.LONG, stake=1000)
    assert len(opened.state.positions) == 1
    await store.close()


async def test_safety_net_grants_once_per_day_until_target(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    await store.save_account(Account(state.session.id, 1_900.0, 1_900.0, 0.0))

    first = await engine.quote("u1", "g1", hebei)
    assert first.safety_grant == pytest.approx(500)
    assert first.account.cash == pytest.approx(2_400)
    assert first.session.safety_active is True

    same_day = await engine.quote("u1", "g1", hebei)
    assert same_day.safety_grant == 0
    assert same_day.account.cash == pytest.approx(2_400)

    now["value"] = at(28, 10, 0)
    second = await engine.quote("u1", "g1", hebei)
    assert second.safety_grant == pytest.approx(500)
    assert second.account.cash == pytest.approx(2_900)

    now["value"] = at(29, 10, 0)
    third = await engine.quote("u1", "g1", hebei)
    assert third.safety_grant == pytest.approx(500)
    assert third.account.cash == pytest.approx(3_400)

    now["value"] = at(30, 10, 0)
    fourth = await engine.quote("u1", "g1", hebei)
    assert fourth.safety_grant == pytest.approx(500)
    assert fourth.account.cash == pytest.approx(3_900)
    assert fourth.session.safety_active is False

    now["value"] = at(31, 10, 0)
    fifth = await engine.quote("u1", "g1", hebei)
    assert fifth.safety_grant == 0
    assert fifth.account.cash == pytest.approx(3_900)
    await store.close()


async def test_safety_net_catches_up_missed_days(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    await store.save_account(Account(state.session.id, 1_900.0, 1_900.0, 0.0))

    first = await engine.quote("u1", "g1", hebei)
    assert first.account.cash == pytest.approx(2_400)

    now["value"] = at(30, 10, 0)
    catch_up = await engine.quote("u1", "g1", hebei)
    assert catch_up.safety_grant == pytest.approx(1_500)
    assert catch_up.account.cash == pytest.approx(3_900)
    assert catch_up.session.safety_active is False
    await store.close()


async def test_board_runs_continuously_without_end(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)

    from datetime import datetime

    now["value"] = datetime(2027, 3, 1, 10, 0, tzinfo=TZ).timestamp()
    later = await engine.quote("u1", "g1", hebei)
    assert later.ended is False
    assert later.session.status.value == "running"
    assert later.season.id == state.season.id
    assert later.relative_bar == state.relative_bar
    assert len(later.wxi) == TOTAL_BARS
    await store.close()


def test_bet_value_and_wipe_price_are_simple() -> None:
    long_position = Position(
        session_id="s", side=Side.LONG, stake=1000, entry_price=1000, province="河北省"
    )
    short_position = Position(
        session_id="s", side=Side.SHORT, stake=1000, entry_price=1000, province="河北省"
    )
    assert _bet_value(long_position, 1100, 10) == pytest.approx(2000)
    assert _bet_value(long_position, 900, 10) == 0
    assert _bet_value(short_position, 900, 10) == pytest.approx(2000)
    assert _bet_value(short_position, 1100, 10) == 0
    assert _wipe_price(long_position, 10) == pytest.approx(900)
    assert _wipe_price(short_position, 10) == pytest.approx(1100)


async def test_quote_head_pressure_table(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    head = engine.quote_head(state, engine.quote_stats(state))
    pressure = head["pressure"]
    assert {row["label"] for row in pressure} == {"气温", "降水", "天气", "露点", "阵风", "空气"}
    total = sum(float(row["contribution"]) for row in pressure)
    assert total == pytest.approx(state.current.comfort - 100.0, abs=1e-6)
    # 主导只有一个，且不能是恒定的空气项
    dominant = [row for row in pressure if row["dominant"]]
    assert len(dominant) == 1
    assert dominant[0]["key"] != "air"
    # 压力组走模板单独渲染，不再塞进 groups（否则图头会多出一列/换行）
    assert len(head["groups"]) == 5
    await store.close()


async def test_pressure_air_never_dominant_on_perfect_weather(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    relative = state.relative_bar
    ideal = WxiBar(
        time=state.wxi.bars[relative].time,
        open=1000.0,
        high=1001.0,
        low=999.0,
        close=1000.0,
        activity=1.0,
        cape=0,
        gust=0.0,
        precip=0.0,
        wcode=0,
        temp=22.0,
        dew=10.0,
        comfort=100.0,
        regional=None,
    )
    bars = list(state.wxi.bars)
    bars[relative] = ideal
    fake = replace(state, wxi=replace(state.wxi, bars=tuple(bars)))
    pressure = engine.comfort_pressure(fake)
    air = next(row for row in pressure if row["key"] == "air")
    assert air["dominant"] is False
    assert air["text"].startswith("固定 ")
    assert "85" not in air["text"]
    await store.close()


async def test_quote_head_high_low_have_units_and_colors(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    head = engine.quote_head(state, engine.quote_stats(state))
    first = {item[0]: item for item in head["groups"][0]}
    assert first["今日最高"][1].endswith("℃")
    assert first["今日最高"][2] == "up"
    assert first["今日最低"][1].endswith("℃")
    assert first["今日最低"][2] == "down"
    # 其余分组仍保持二元组，模板向后兼容
    assert any(len(item) == 2 for group in head["groups"][1:] for item in group)
    await store.close()


async def test_buy_by_shares_and_amount_conversion(tmp_path: Path) -> None:
    now = {"value": at(27, 10, 0)}
    engine, store, _ = await make_engine(tmp_path, now)
    hebei = resolve_province("河北")
    assert hebei is not None
    state = await engine.ensure_game("u1", "g1", hebei)
    price = state.current.close
    leverage = state.session.leverage

    exact = await engine.open_position("u1", "g1", hebei, Side.LONG, qty=100)
    assert exact.trade.qty == 100
    assert exact.trade.stake == pytest.approx(100 * price / leverage)
    assert exact.state.positions[0].qty == 100
    assert exact.state.positions[0].margin == pytest.approx(exact.trade.stake)

    with pytest.raises(GameError, match="最少"):
        await engine.open_position("u2", "g1", hebei, Side.LONG, qty=1)

    # 金额路径：floor 成份数，保证金随之回落
    other = await engine.open_position("u3", "g1", hebei, Side.LONG, stake=1000)
    assert other.trade.qty == int(1000 * leverage / price)
    assert other.trade.stake <= 1000
    assert other.trade.margin == pytest.approx(other.trade.stake)

    closed = await engine.close_position("u3", "g1", hebei)
    assert closed.trades[0].qty == other.trade.qty
    assert closed.trades[0].margin == pytest.approx(other.trade.stake)
    await store.close()
