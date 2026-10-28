from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from ..models import (
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
from .openmeteo import MinutelySeries, RegionSeries

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS seasons (
  id TEXT PRIMARY KEY,
  province TEXT NOT NULL,
  city TEXT NOT NULL,
  seed INTEGER NOT NULL,
  start_bar INTEGER NOT NULL,
  end_bar INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  owner TEXT NOT NULL DEFAULT '',
  group_id TEXT,
  current_bar INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  speed REAL NOT NULL DEFAULT 1,
  start_time TEXT NOT NULL DEFAULT '',
  end_time TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  group_id TEXT,
  season_id TEXT NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
  principal REAL NOT NULL,
  leverage REAL NOT NULL,
  current_bar INTEGER NOT NULL,
  status TEXT NOT NULL,
  speed REAL NOT NULL DEFAULT 1,
  return_pct REAL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  province TEXT NOT NULL DEFAULT '',
  safety_active INTEGER NOT NULL DEFAULT 0,
  safety_date TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  cash REAL NOT NULL,
  equity REAL NOT NULL,
  stake REAL NOT NULL DEFAULT 0,
  margin_used REAL NOT NULL DEFAULT 0,
  margin_frozen REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  province TEXT NOT NULL,
  side TEXT NOT NULL,
  stake REAL NOT NULL DEFAULT 0,
  entry_price REAL NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,
  margin REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, province)
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  side TEXT NOT NULL,
  stake REAL NOT NULL DEFAULT 0,
  price REAL NOT NULL,
  status TEXT NOT NULL,
  province TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  bar_index INTEGER NOT NULL,
  side TEXT NOT NULL,
  stake REAL NOT NULL DEFAULT 0,
  price REAL NOT NULL,
  pnl REAL NOT NULL DEFAULT 0,
  province TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 0,
  fee REAL NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS market_series (
  season_id TEXT NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
  province TEXT NOT NULL,
  city_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (season_id, province)
);

CREATE TABLE IF NOT EXISTS market_regions (
  season_id TEXT NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
  province TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (season_id, province)
);

CREATE TABLE IF NOT EXISTS leaderboard (
  season_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  group_id TEXT,
  return_pct REAL NOT NULL,
  equity REAL NOT NULL,
  max_drawdown REAL NOT NULL,
  trades INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (season_id, user_id, group_id)
);

CREATE TABLE IF NOT EXISTS season_series (
  season_id TEXT PRIMARY KEY REFERENCES seasons(id) ON DELETE CASCADE,
  city_id TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS region_series (
  season_id TEXT PRIMARY KEY REFERENCES seasons(id) ON DELETE CASCADE,
  province TEXT NOT NULL,
  cities TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_season ON sessions(season_id);
CREATE INDEX IF NOT EXISTS idx_orders_session_status ON orders(session_id, status);
CREATE INDEX IF NOT EXISTS idx_trades_session_bar ON trades(session_id, bar_index);
CREATE INDEX IF NOT EXISTS idx_leaderboard_season ON leaderboard(season_id);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.path)
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            self._db = db
        return self._db

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store 尚未连接")
        return self._db

    async def initialize(self) -> None:
        db = await self.connect()
        await db.executescript(SCHEMA)
        for table, name, ddl in (
            ("seasons", "owner", "TEXT NOT NULL DEFAULT ''"),
            ("seasons", "group_id", "TEXT"),
            ("seasons", "current_bar", "INTEGER NOT NULL DEFAULT 0"),
            ("seasons", "updated_at", "INTEGER NOT NULL DEFAULT 0"),
            ("seasons", "speed", "REAL NOT NULL DEFAULT 1"),
            ("seasons", "start_time", "TEXT NOT NULL DEFAULT ''"),
            ("seasons", "end_time", "TEXT NOT NULL DEFAULT ''"),
            ("sessions", "speed", "REAL NOT NULL DEFAULT 1"),
            ("sessions", "return_pct", "REAL"),
            ("sessions", "province", "TEXT NOT NULL DEFAULT ''"),
            ("sessions", "safety_active", "INTEGER NOT NULL DEFAULT 0"),
            ("sessions", "safety_date", "TEXT NOT NULL DEFAULT ''"),
            ("accounts", "stake", "REAL NOT NULL DEFAULT 0"),
            ("positions", "stake", "REAL NOT NULL DEFAULT 0"),
            ("positions", "province", "TEXT NOT NULL DEFAULT ''"),
            ("orders", "stake", "REAL NOT NULL DEFAULT 0"),
            ("orders", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "province", "TEXT NOT NULL DEFAULT ''"),
            ("trades", "stake", "REAL NOT NULL DEFAULT 0"),
            ("trades", "pnl", "REAL NOT NULL DEFAULT 0"),
            ("trades", "province", "TEXT NOT NULL DEFAULT ''"),
        ):
            existing = {
                str(row[1]) for row in await self._fetchall_raw(f"PRAGMA table_info({table})")
            }
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        position_rows = await self._fetchall_raw("PRAGMA table_info(positions)")
        pk_columns = {str(row[1]) for row in position_rows if int(row[5]) > 0}
        if pk_columns != {"session_id", "province"}:
            await db.execute("ALTER TABLE positions RENAME TO positions_old")
            await db.execute(
                """
                CREATE TABLE positions (
                  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                  province TEXT NOT NULL,
                  side TEXT NOT NULL,
                  stake REAL NOT NULL DEFAULT 0,
                  entry_price REAL NOT NULL,
                  qty INTEGER NOT NULL DEFAULT 0,
                  margin REAL NOT NULL DEFAULT 0,
                  PRIMARY KEY (session_id, province)
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO positions
                  (session_id, province, side, stake, entry_price, qty, margin)
                SELECT session_id, province, side, stake, entry_price, qty, margin
                FROM positions_old
                """
            )
            await db.execute("DROP TABLE positions_old")
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()

    async def _fetchall_raw(self, sql: str) -> list[Any]:
        db = await self.connect()
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        db = await self.connect()
        await db.execute(sql, params)
        await db.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        db = await self.connect()
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        db = await self.connect()
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def add_season(self, season: Season) -> None:
        await self.execute(
            """
            INSERT INTO seasons
              (id, province, city, seed, start_bar, end_bar, status, created_at,
               owner, group_id, current_bar, updated_at, speed, start_time, end_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                season.id,
                season.province,
                season.city,
                season.seed,
                season.start_bar,
                season.end_bar,
                season.status.value,
                season.created_at,
                season.owner,
                season.group_id,
                season.current_bar,
                season.updated_at,
                season.speed,
                season.start_time,
                season.end_time,
            ),
        )

    async def get_season(self, season_id: str) -> Season | None:
        row = await self.fetchone("SELECT * FROM seasons WHERE id = ?", (season_id,))
        if row is None:
            return None
        return Season(
            id=str(row["id"]),
            province=str(row["province"]),
            city=str(row["city"]),
            seed=int(row["seed"]),
            start_bar=int(row["start_bar"]),
            end_bar=int(row["end_bar"]),
            status=SeasonStatus(str(row["status"])),
            created_at=int(row["created_at"]),
            owner=str(row["owner"]),
            group_id=str(row["group_id"]) if row["group_id"] is not None else None,
            current_bar=int(row["current_bar"]),
            updated_at=int(row["updated_at"]),
            speed=float(row["speed"]),
            start_time=str(row["start_time"]),
            end_time=str(row["end_time"]),
        )

    async def get_running_season_for_owner(self, owner: str) -> Season | None:
        row = await self.fetchone(
            """
            SELECT * FROM seasons
            WHERE owner = ? AND status = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (owner, SeasonStatus.RUNNING.value),
        )
        if row is None:
            return None
        return await self.get_season(str(row["id"]))

    async def update_season_clock(
        self, season_id: str, *, current_bar: int, updated_at: int
    ) -> None:
        await self.execute(
            "UPDATE seasons SET current_bar = ?, updated_at = ? WHERE id = ?",
            (current_bar, updated_at, season_id),
        )

    async def add_session(self, session: Session) -> None:
        await self.execute(
            """
            INSERT INTO sessions
              (id, user_id, group_id, season_id, principal, leverage,
               current_bar, status, speed, return_pct, created_at, updated_at,
               province, safety_active, safety_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.user_id,
                session.group_id,
                session.season_id,
                session.principal,
                session.leverage,
                session.current_bar,
                session.status.value,
                session.speed,
                session.return_pct,
                session.created_at,
                session.updated_at,
                session.province,
                int(session.safety_active),
                session.safety_date,
            ),
        )

    async def get_session(self, session_id: str) -> Session | None:
        row = await self.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return Session(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]) if row["group_id"] is not None else None,
            season_id=str(row["season_id"]),
            principal=float(row["principal"]),
            leverage=float(row["leverage"]),
            current_bar=int(row["current_bar"]),
            status=SessionStatus(str(row["status"])),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            speed=float(row["speed"]),
            return_pct=float(row["return_pct"]) if row["return_pct"] is not None else None,
            province=str(row["province"]),
            safety_active=bool(row["safety_active"]),
            safety_date=str(row["safety_date"]),
        )

    async def get_latest_session_on_board(
        self, user_id: str, season_id: str, group_id: str | None
    ) -> Session | None:
        row = await self.fetchone(
            """
            SELECT * FROM sessions
            WHERE user_id = ? AND season_id = ? AND group_id IS ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, season_id, group_id),
        )
        if row is None:
            return None
        return await self.get_session(str(row["id"]))

    async def get_latest_running_session_on_board(
        self, user_id: str, season_id: str, group_id: str | None
    ) -> Session | None:
        row = await self.fetchone(
            """
            SELECT * FROM sessions
            WHERE user_id = ? AND season_id = ? AND status = ? AND group_id IS ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, season_id, SessionStatus.RUNNING.value, group_id),
        )
        if row is None:
            return None
        return await self.get_session(str(row["id"]))

    async def update_session(
        self,
        session_id: str,
        *,
        current_bar: int,
        updated_at: int,
        status: SessionStatus | None = None,
    ) -> None:
        if status is None:
            await self.execute(
                "UPDATE sessions SET current_bar = ?, updated_at = ? WHERE id = ?",
                (current_bar, updated_at, session_id),
            )
            return
        await self.execute(
            """
            UPDATE sessions
            SET current_bar = ?, updated_at = ?, status = ?
            WHERE id = ?
            """,
            (current_bar, updated_at, status.value, session_id),
        )

    async def set_session_province(self, session_id: str, province: str) -> None:
        await self.execute(
            "UPDATE sessions SET province = ? WHERE id = ?",
            (province, session_id),
        )

    async def set_session_leverage(self, session_id: str, leverage: float, updated_at: int) -> None:
        await self.execute(
            "UPDATE sessions SET leverage = ?, updated_at = ? WHERE id = ?",
            (leverage, updated_at, session_id),
        )

    async def set_session_safety(self, session_id: str, *, active: bool, grant_date: str) -> None:
        await self.execute(
            "UPDATE sessions SET safety_active = ?, safety_date = ? WHERE id = ?",
            (int(active), grant_date, session_id),
        )

    async def settle_session(
        self,
        session_id: str,
        *,
        return_pct: float,
        updated_at: int,
        status: SessionStatus,
    ) -> None:
        await self.execute(
            """
            UPDATE sessions
            SET status = ?, return_pct = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, return_pct, updated_at, session_id),
        )

    async def get_account(self, session_id: str) -> Account | None:
        row = await self.fetchone("SELECT * FROM accounts WHERE session_id = ?", (session_id,))
        if row is None:
            return None
        return Account(
            session_id=str(row["session_id"]),
            cash=float(row["cash"]),
            equity=float(row["equity"]),
            stake=float(row["stake"]),
        )

    async def save_account(self, account: Account) -> None:
        await self.execute(
            """
            INSERT INTO accounts
              (session_id, cash, equity, stake, margin_used, margin_frozen)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(session_id) DO UPDATE SET
              cash = excluded.cash,
              equity = excluded.equity,
              stake = excluded.stake,
              margin_used = excluded.margin_used,
              margin_frozen = 0
            """,
            (
                account.session_id,
                account.cash,
                account.equity,
                account.stake,
                account.stake,
            ),
        )

    async def get_positions(self, session_id: str) -> list[Position]:
        rows = await self.fetchall(
            "SELECT * FROM positions WHERE session_id = ? ORDER BY province",
            (session_id,),
        )
        return [
            Position(
                session_id=str(row["session_id"]),
                side=Side(str(row["side"])),
                stake=float(row["stake"]),
                entry_price=float(row["entry_price"]),
                province=str(row["province"]),
            )
            for row in rows
        ]

    async def get_position(self, session_id: str, province: str) -> Position | None:
        row = await self.fetchone(
            "SELECT * FROM positions WHERE session_id = ? AND province = ?",
            (session_id, province),
        )
        if row is None:
            return None
        return Position(
            session_id=str(row["session_id"]),
            side=Side(str(row["side"])),
            stake=float(row["stake"]),
            entry_price=float(row["entry_price"]),
            province=str(row["province"]),
        )

    async def save_position(self, position: Position) -> None:
        await self.execute(
            """
            INSERT INTO positions
              (session_id, province, side, stake, entry_price, qty, margin)
            VALUES (?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(session_id, province) DO UPDATE SET
              side = excluded.side,
              stake = excluded.stake,
              entry_price = excluded.entry_price,
              qty = 0,
              margin = 0
            """,
            (
                position.session_id,
                position.province,
                position.side.value,
                position.stake,
                position.entry_price,
            ),
        )

    async def delete_position(self, session_id: str, province: str) -> None:
        await self.execute(
            "DELETE FROM positions WHERE session_id = ? AND province = ?",
            (session_id, province),
        )

    async def delete_all_positions(self, session_id: str) -> None:
        await self.execute("DELETE FROM positions WHERE session_id = ?", (session_id,))

    async def add_order(self, order: Order) -> None:
        await self.execute(
            """
            INSERT INTO orders
              (id, session_id, kind, side, stake, price, status, province, qty, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                order.id,
                order.session_id,
                order.kind.value,
                order.side.value,
                order.stake,
                order.price,
                order.status.value,
                order.province,
                order.created_at,
            ),
        )

    async def list_open_orders(self, session_id: str) -> list[Order]:
        rows = await self.fetchall(
            """
            SELECT * FROM orders
            WHERE session_id = ? AND status = ?
            ORDER BY created_at ASC
            """,
            (session_id, OrderStatus.OPEN.value),
        )
        return [
            Order(
                id=str(row["id"]),
                session_id=str(row["session_id"]),
                kind=OrderKind(str(row["kind"])),
                side=Side(str(row["side"])),
                stake=float(row["stake"]),
                price=float(row["price"]),
                status=OrderStatus(str(row["status"])),
                created_at=int(row["created_at"]),
                province=str(row["province"]),
            )
            for row in rows
        ]

    async def set_order_status(self, order_id: str, status: OrderStatus) -> None:
        await self.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (status.value, order_id),
        )

    async def cancel_open_orders(self, session_id: str) -> int:
        db = await self.connect()
        cursor = await db.execute(
            "UPDATE orders SET status = ? WHERE session_id = ? AND status = ?",
            (OrderStatus.CANCELLED.value, session_id, OrderStatus.OPEN.value),
        )
        await db.commit()
        count = cursor.rowcount
        await cursor.close()
        return int(count)

    async def add_trade(self, trade: Trade) -> None:
        await self.execute(
            """
            INSERT INTO trades
              (id, session_id, bar_index, side, stake, price, pnl, province,
               qty, fee, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                trade.id,
                trade.session_id,
                trade.bar_index,
                trade.side.value,
                trade.stake,
                trade.price,
                trade.pnl,
                trade.province,
                trade.created_at,
            ),
        )

    async def list_trades(self, session_id: str, limit: int = 20) -> list[Trade]:
        rows = await self.fetchall(
            """
            SELECT * FROM trades
            WHERE session_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (session_id, limit),
        )
        return [
            Trade(
                id=str(row["id"]),
                session_id=str(row["session_id"]),
                bar_index=int(row["bar_index"]),
                side=Side(str(row["side"])),
                stake=float(row["stake"]),
                price=float(row["price"]),
                pnl=float(row["pnl"]),
                created_at=int(row["created_at"]),
                province=str(row["province"]),
            )
            for row in rows
        ]

    async def save_series(self, season_id: str, series: MinutelySeries) -> None:
        if not series.time:
            raise ValueError("行情序列为空")
        await self.execute(
            """
            INSERT INTO season_series
              (season_id, city_id, start_time, end_time, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
              city_id = excluded.city_id,
              start_time = excluded.start_time,
              end_time = excluded.end_time,
              payload = excluded.payload,
              fetched_at = excluded.fetched_at
            """,
            (
                season_id,
                series.city_id,
                series.time[0],
                series.time[-1],
                series.to_json(),
                int(series.fetched_at),
            ),
        )

    async def load_series(self, season_id: str) -> MinutelySeries | None:
        row = await self.fetchone(
            "SELECT payload FROM season_series WHERE season_id = ?", (season_id,)
        )
        if row is None:
            return None
        return MinutelySeries.from_json(str(row["payload"]))

    async def save_region_series(self, season_id: str, region: RegionSeries) -> None:
        if not region.time:
            raise ValueError("区域行情序列为空")
        await self.execute(
            """
            INSERT INTO region_series
              (season_id, province, cities, start_time, end_time, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
              province = excluded.province,
              cities = excluded.cities,
              start_time = excluded.start_time,
              end_time = excluded.end_time,
              payload = excluded.payload,
              fetched_at = excluded.fetched_at
            """,
            (
                season_id,
                region.province,
                json.dumps(list(region.cities), ensure_ascii=False),
                region.time[0],
                region.time[-1],
                region.to_json(),
                int(region.fetched_at),
            ),
        )

    async def load_region_series(self, season_id: str) -> RegionSeries | None:
        row = await self.fetchone(
            "SELECT payload FROM region_series WHERE season_id = ?", (season_id,)
        )
        if row is None:
            return None
        return RegionSeries.from_json(str(row["payload"]))

    async def save_market_series(
        self, season_id: str, province: str, series: MinutelySeries
    ) -> None:
        if not series.time:
            raise ValueError("行情序列为空")
        await self.execute(
            """
            INSERT INTO market_series (season_id, province, city_id, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season_id, province) DO UPDATE SET
              city_id = excluded.city_id,
              payload = excluded.payload,
              fetched_at = excluded.fetched_at
            """,
            (
                season_id,
                province,
                series.city_id,
                series.to_json(),
                int(series.fetched_at),
            ),
        )

    async def load_market_series(self, season_id: str, province: str) -> MinutelySeries | None:
        row = await self.fetchone(
            "SELECT payload FROM market_series WHERE season_id = ? AND province = ?",
            (season_id, province),
        )
        if row is None:
            return None
        return MinutelySeries.from_json(str(row["payload"]))

    async def save_market_region(self, season_id: str, province: str, region: RegionSeries) -> None:
        if not region.time:
            raise ValueError("区域行情序列为空")
        await self.execute(
            """
            INSERT INTO market_regions (season_id, province, payload, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(season_id, province) DO UPDATE SET
              payload = excluded.payload,
              fetched_at = excluded.fetched_at
            """,
            (season_id, province, region.to_json(), int(region.fetched_at)),
        )

    async def load_market_region(self, season_id: str, province: str) -> RegionSeries | None:
        row = await self.fetchone(
            "SELECT payload FROM market_regions WHERE season_id = ? AND province = ?",
            (season_id, province),
        )
        if row is None:
            return None
        return RegionSeries.from_json(str(row["payload"]))

    async def table_names(self) -> list[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return sorted(str(row["name"]) for row in rows)


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        from nonebot_plugin_localstore import get_plugin_data_file

        _store = Store(get_plugin_data_file("game.db"))
    return _store


async def init_store() -> None:
    await get_store().initialize()


async def close_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
