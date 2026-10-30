from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nonebot_plugin_weather_fighter.models import (
    Account,
    Order,
    OrderKind,
    OrderStatus,
    Position,
    Season,
    SeasonStatus,
    Session,
    SessionStatus,
    Side,
    Trade,
)
from nonebot_plugin_weather_fighter.services.openmeteo import MinutelySeries, RegionSeries
from nonebot_plugin_weather_fighter.services.store import SCHEMA, Store

TABLES = {
    "accounts",
    "leaderboard",
    "market_regions",
    "market_series",
    "orders",
    "positions",
    "region_series",
    "season_series",
    "seasons",
    "sessions",
    "trades",
}


def series() -> MinutelySeries:
    return MinutelySeries(
        city_id="101090101",
        city_name="石家庄",
        time=("2026-10-20T00:00", "2026-10-20T00:15"),
        temp=(20.0, 20.5),
        gust=(5.0, 6.0),
        precip=(0.0, 0.2),
        wcode=(0, 61),
        cape=(100.0, 200.0),
        dew=(10.0, 10.5),
        fetched_at=1000.0,
        source="test",
    )


def region() -> RegionSeries:
    return RegionSeries(
        province="河北省",
        scope="河北",
        cities=("石家庄", "保定"),
        time=("2026-10-20T00:00", "2026-10-20T00:15"),
        temp=(20.0, 20.5),
        gust=(5.0, 6.0),
        precip=(0.0, 0.2),
        wcode=(0.0, 61.0),
        cape=(100.0, 200.0),
        dew=(10.0, 10.5),
        fetched_at=1000.0,
        source="test",
    )


async def test_tables_and_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "game.db")
    await store.initialize()
    assert set(await store.table_names()) == TABLES

    season = Season(
        id="s1",
        province="河北省",
        city="石家庄",
        seed=7,
        start_bar=0,
        end_bar=100,
        status=SeasonStatus.RUNNING,
        created_at=1000,
    )
    await store.add_season(season)
    assert await store.get_season("s1") == season

    session = Session(
        id="g1",
        user_id="42",
        group_id="100",
        season_id="s1",
        principal=10000.0,
        leverage=5.0,
        current_bar=0,
        status=SessionStatus.RUNNING,
        created_at=1000,
        updated_at=1000,
        speed=45.0,
    )
    await store.add_session(session)
    assert await store.get_session("g1") == session

    account = Account(session_id="g1", cash=70_000.0, equity=70_000.0, stake=30_000.0)
    await store.save_account(account)
    assert await store.get_account("g1") == account

    position = Position(
        session_id="g1",
        side=Side.LONG,
        stake=30_000.0,
        entry_price=1010.0,
        province="河北省",
        qty=29,
        margin=30_000.0,
    )
    await store.save_position(position)
    assert await store.get_positions("g1") == [position]
    assert await store.get_position("g1", "河北省") == position

    second_position = Position(
        session_id="g1",
        side=Side.SHORT,
        stake=10_000.0,
        entry_price=1020.0,
        province="山东省",
        qty=9,
        margin=10_000.0,
    )
    await store.save_position(second_position)
    assert len(await store.get_positions("g1")) == 2
    await store.delete_position("g1", "河北省")
    assert await store.get_positions("g1") == [second_position]

    order = Order(
        id="o1",
        session_id="g1",
        kind=OrderKind.BET,
        side=Side.LONG,
        stake=30_000.0,
        price=1020.0,
        status=OrderStatus.OPEN,
        created_at=1000,
        province="河北省",
        qty=176,
        margin=30_000.0,
    )
    await store.add_order(order)
    assert await store.list_open_orders("g1") == [order]
    await store.set_order_status("o1", OrderStatus.FILLED)
    assert await store.list_open_orders("g1") == []

    trade = Trade(
        id="t1",
        session_id="g1",
        bar_index=7,
        side=Side.LONG,
        stake=30_000.0,
        price=1020.0,
        pnl=1_200.0,
        created_at=1000,
        province="河北省",
        qty=29,
        margin=30_000.0,
    )
    await store.add_trade(trade)
    assert await store.list_trades("g1") == [trade]

    payload = series()
    await store.save_series("s1", payload)
    loaded = await store.load_series("s1")
    assert loaded is not None
    assert loaded.to_dict() == payload.to_dict()

    region_payload = region()
    await store.save_region_series("s1", region_payload)
    loaded_region = await store.load_region_series("s1")
    assert loaded_region is not None
    assert loaded_region.to_dict() == region_payload.to_dict()

    await store.save_market_series("s1", "河北省", payload)
    await store.save_market_region("s1", "河北省", region_payload)
    loaded_market = await store.load_market_series("s1", "河北省")
    loaded_market_region = await store.load_market_region("s1", "河北省")
    assert loaded_market is not None
    assert loaded_market.to_dict() == payload.to_dict()
    assert loaded_market_region is not None
    assert loaded_market_region.to_dict() == region_payload.to_dict()

    await store.close()


async def test_initialize_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "game.db")
    await store.initialize()
    await store.initialize()
    assert set(await store.table_names()) == TABLES
    row = await store.fetchone("PRAGMA user_version")
    assert row is not None and int(row["user_version"]) == 7
    await store.close()


async def test_migrates_m0_sessions_table(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          group_id TEXT,
          season_id TEXT NOT NULL,
          principal REAL NOT NULL,
          leverage REAL NOT NULL,
          current_bar INTEGER NOT NULL,
          status TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    store = Store(path)
    await store.initialize()
    columns = {str(row["name"]) for row in await store.fetchall("PRAGMA table_info(sessions)")}
    assert {"speed", "return_pct"} <= columns
    await store.close()


async def test_migrate_v6_backfills_qty_and_margin(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "v6.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    # v6: positions 已有 qty/margin；orders/trades 只有 qty，没有 margin
    connection.execute("ALTER TABLE orders DROP COLUMN margin")
    connection.execute("ALTER TABLE trades DROP COLUMN margin")
    connection.execute("PRAGMA user_version = 6")
    connection.execute(
        """
        INSERT INTO sessions
          (id, user_id, group_id, season_id, principal, leverage, current_bar,
           status, created_at, updated_at)
        VALUES ('s1', 'u1', 'g1', 'season1', 10000, 10, 0, 'running', 0, 0)
        """
    )
    connection.execute(
        """
        INSERT INTO positions (session_id, province, side, stake, entry_price, qty, margin)
        VALUES ('s1', '河北省', 'long', 1000, 100, 0, 0)
        """
    )
    connection.execute(
        """
        INSERT INTO orders
          (id, session_id, kind, side, stake, price, status, province, qty, created_at)
        VALUES ('o1', 's1', 'bet', 'long', 500, 50, 'open', '河北省', 0, 0)
        """
    )
    connection.execute(
        """
        INSERT INTO trades
          (id, session_id, bar_index, side, stake, price, pnl, province, qty, fee, created_at)
        VALUES ('t1', 's1', 0, 'long', 200, 25, 0, '河北省', 0, 0, 0)
        """
    )
    connection.commit()
    connection.close()

    store = Store(path)
    await store.initialize()
    positions = await store.get_positions("s1")
    assert positions[0].qty == 100
    assert positions[0].margin == 1000.0
    orders = await store.list_open_orders("s1")
    assert orders[0].qty == 100
    assert orders[0].margin == 500.0
    trades = await store.list_trades("s1")
    assert trades[0].qty == 80
    assert trades[0].margin == 200.0
    row = await store.fetchone("PRAGMA user_version")
    assert row is not None and int(row["user_version"]) == 7
    await store.close()


async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    import sqlite3

    store = Store(tmp_path / "game.db")
    await store.initialize()
    season = Season(
        id="s1",
        province="河北省",
        city="石家庄",
        seed=1,
        start_bar=0,
        end_bar=1,
        status=SeasonStatus.RUNNING,
        created_at=0,
    )
    await store.add_season(season)
    session = Session(
        id="sess",
        user_id="u1",
        group_id="g1",
        season_id="s1",
        principal=10_000,
        leverage=10,
        current_bar=0,
        status=SessionStatus.RUNNING,
        created_at=0,
        updated_at=0,
    )
    await store.add_session(session)
    valid = Trade(
        id="t1",
        session_id="sess",
        bar_index=0,
        side=Side.LONG,
        stake=100,
        price=1,
        pnl=0,
        created_at=0,
        province="河北省",
        qty=1,
        margin=100,
    )
    invalid = replace(valid, id="t2", session_id="missing")

    with pytest.raises(sqlite3.IntegrityError):
        async with store.transaction() as txn:
            await txn.add_trade(valid)
            await txn.add_trade(invalid)

    assert await store.list_trades("sess") == []
    await store.close()
