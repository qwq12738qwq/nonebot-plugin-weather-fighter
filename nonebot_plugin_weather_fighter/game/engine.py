from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import wraps
from typing import Any, Concatenate, ParamSpec, TypeVar

from nonebot import logger

from .. import consts
from ..models import (
    Account,
    Bar,
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
from ..services.market_time import (
    BARS_PER_DAY,
    TZ,
    elapsed_bars,
    market_status,
)
from ..services.openmeteo import (
    OpenMeteoClient,
    merge_minutely,
    prune_minutely,
)
from ..services.provinces import Province, resolve_province
from ..services.regions import region_cities
from ..services.store import Store
from .wxi import (
    COMFORT_WEIGHTS,
    TRADE_DAYS,
    WARM_DAYS,
    WxiBar,
    WxiSeries,
    bar_scores,
    build_wxi_series,
)

DEFAULT_PRINCIPAL = 10_000.0
MAX_LEVERAGE = 100.0
INITIAL_LEVERAGE = 1.0
MIN_STAKE = 100.0
DEFAULT_SAFETY_ENTRY = 0.20
DEFAULT_SAFETY_GRANT = 0.05
DEFAULT_SAFETY_TARGET = 0.35
DISPLAY_WICK_FACTOR = 0.8
CHART_BARS = 96
TOTAL_BARS = (WARM_DAYS + TRADE_DAYS) * BARS_PER_DAY
REFRESH_SECONDS = 30 * 60


class GameError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameState:
    session: Session
    season: Season
    wxi: WxiSeries
    account: Account
    positions: tuple[Position, ...] = ()
    orders: tuple[Order, ...] = ()
    position_prices: tuple[tuple[str, float], ...] = ()
    safety_grant: float = 0.0
    ended: bool = False

    @property
    def relative_bar(self) -> int:
        return max(0, min(self.season.current_bar, len(self.wxi) - 1))

    @property
    def current(self) -> WxiBar:
        relative = self.relative_bar
        previous = self.wxi.bars[relative - 1].temp if relative > 0 else None
        return _display_bar(self.wxi.bars[relative], previous)

    def price_for(self, position: Position) -> float:
        for province, price in self.position_prices:
            if province == position.province:
                return price
        return self.current.close

    def pnl_for(self, position: Position) -> float:
        return (
            _bet_value(position, self.price_for(position), self.session.leverage) - position.stake
        )

    @property
    def position_value(self) -> float:
        return sum(
            _bet_value(position, self.price_for(position), self.session.leverage)
            for position in self.positions
        )

    @property
    def position_pnl(self) -> float:
        return self.position_value - sum(position.stake for position in self.positions)

    @property
    def total_stake(self) -> float:
        return sum(position.stake for position in self.positions)


@dataclass(frozen=True)
class QuoteStats:
    price: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    ma5: float | None
    ma20: float | None
    activity: float
    regional: float | None
    regional_change: float | None
    day: int
    time_label: str
    session_label: str
    day_open: float
    prev_close: float | None
    day_high: float
    day_low: float
    amplitude: float
    market_status: str
    market_open: bool
    temp_high: float
    temp_low: float
    temp_amplitude: float
    feels_like: float | None
    humidity: float | None
    day_open_temp: float
    prev_day_close_temp: float | None
    prev_day_high: float | None
    prev_day_low: float | None
    observations: int


@dataclass(frozen=True)
class ChartData:
    bars: tuple[Bar, ...]
    regional: tuple[float | None, ...]
    ma5: tuple[float | None, ...]
    ma20: tuple[float | None, ...]
    current_price: float
    forecast: tuple[Bar, ...] = ()


@dataclass(frozen=True)
class TradeOutcome:
    state: GameState
    trade: Trade


@dataclass(frozen=True)
class OrderOutcome:
    state: GameState
    order: Order


@dataclass(frozen=True)
class CloseOutcome:
    state: GameState | None
    trades: tuple[Trade, ...]
    total_pnl: float


def _moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index >= window - 1:
            out[index] = total / window
    return out


def _bet_value(position: Position, price: float, leverage: float) -> float:
    if position.entry_price <= 0:
        return 0.0
    move = (price - position.entry_price) / position.entry_price
    if position.side is Side.SHORT:
        move = -move
    return max(0.0, position.stake * (1.0 + leverage * move))


def _wipe_price(position: Position, leverage: float) -> float:
    if leverage <= 0:
        return 0.0
    if position.side is Side.LONG:
        return position.entry_price * (1.0 - 1.0 / leverage)
    return position.entry_price * (1.0 + 1.0 / leverage)


def _hit(order: Order, bar: WxiBar) -> bool:
    return bar.low <= order.price <= bar.high


def _display_bar(bar: WxiBar, previous_temp: float | None) -> WxiBar:
    """把 WXI 波动映射成气温 K 线：收盘=实际气温，影线借用 WXI 振幅。"""
    close = float(bar.temp)
    open_price = close if previous_temp is None else float(previous_temp)
    span = (bar.high - bar.low) / max(bar.close, 1e-9)
    wick = max(0.05, span * max(abs(close), 1.0) * DISPLAY_WICK_FACTOR)
    high = max(open_price, close) + wick
    low = min(open_price, close) - wick
    regional = bar.regional
    return replace(
        bar,
        open=open_price,
        high=high,
        low=low,
        close=close,
        regional=regional,
    )


def _humidity(temp: float, dew: float | None) -> float | None:
    if dew is None:
        return None
    value = 100.0 * math.exp((17.625 * dew) / (243.04 + dew) - (17.625 * temp) / (243.04 + temp))
    return max(0.0, min(100.0, value))


def _feels_like(temp: float, humidity: float | None, wind: float) -> float | None:
    if humidity is None:
        return None
    vapor = (humidity / 100.0) * 6.105 * math.exp(17.27 * temp / (237.7 + temp))
    return temp + 0.33 * vapor - 0.70 * wind - 4.0


BOARD_OWNER = "global"


def _stable_seed(*parts: str) -> int:
    payload = "|".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFFFFFF_FFFF


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _player_locked(
    method: Callable[Concatenate[GameEngine, str, str | None, _P], Awaitable[_R]],
) -> Callable[Concatenate[GameEngine, str, str | None, _P], Awaitable[_R]]:
    """串行化同一玩家在同一群的引擎动作，避免并发重复成交/丢更新。"""

    @wraps(method)
    async def wrapper(
        self: GameEngine,
        user_id: str,
        group_id: str | None,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        async with self._lock_for_player(user_id, group_id):
            return await method(self, user_id, group_id, *args, **kwargs)

    return wrapper


class GameEngine:
    def __init__(
        self,
        store: Store,
        client: OpenMeteoClient,
        *,
        default_principal: float = DEFAULT_PRINCIPAL,
        max_leverage: float = MAX_LEVERAGE,
        safety_entry_ratio: float = DEFAULT_SAFETY_ENTRY,
        safety_grant_ratio: float = DEFAULT_SAFETY_GRANT,
        safety_target_ratio: float = DEFAULT_SAFETY_TARGET,
        chart_bars: int = CHART_BARS,
        region_enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.default_principal = float(default_principal)
        self.max_leverage = float(max_leverage)
        self.safety_entry_ratio = float(safety_entry_ratio)
        self.safety_grant_ratio = float(safety_grant_ratio)
        self.safety_target_ratio = float(safety_target_ratio)
        self.chart_bars = chart_bars
        self.region_enabled = region_enabled
        self._clock = clock
        self._wxi_cache: OrderedDict[tuple[str, str, str], WxiSeries] = OrderedDict()
        self._board_locks: dict[str, asyncio.Lock] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 开盘：所有群共用一个盘；玩家只是这个盘里的一个账户
    # ------------------------------------------------------------------
    @_player_locked
    async def ensure_game(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> GameState:
        return await self._ensure_game_locked(user_id, group_id, province)

    async def _ensure_game_locked(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> GameState:
        board = await self.store.get_running_season_for_owner(BOARD_OWNER)
        session = (
            await self.store.get_latest_running_session_on_board(user_id, board.id, group_id)
            if board is not None
            else None
        )

        selected = province
        if selected is None and session is not None:
            selected = resolve_province(session.province)
        if selected is None and board is not None:
            selected = resolve_province(board.province)
        if selected is None:
            raise GameError("请指定省份，例如：买入 河北 1000")

        if board is None:
            board = await self._ensure_board(selected)
            session = None

        session = await self._ensure_player(board, session, user_id, group_id, selected)
        board = await self._advance_board(board)
        session = await self._advance_player(session, board)
        account = await self.store.get_account(session.id)
        if account is None:
            account = Account(session.id, session.principal, session.principal, 0.0)
        session, safety_grant = await self._apply_safety_net(session, account, int(self._clock()))
        return await self._state_for(session, board, selected, safety_grant=safety_grant)

    async def quote(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> GameState:
        return await self.ensure_game(user_id, group_id, province)

    async def open_board(self, province: Province) -> Season:
        return await self._ensure_board(province)

    async def last_province(self, user_id: str, group_id: str | None) -> Province | None:
        board = await self.store.get_running_season_for_owner(BOARD_OWNER)
        if board is None:
            return None
        session = await self.store.get_latest_running_session_on_board(user_id, board.id, group_id)
        if session is None or not session.province:
            return None
        return resolve_province(session.province)

    @_player_locked
    async def set_leverage(self, user_id: str, group_id: str | None, leverage: float) -> GameState:
        if leverage < 1 or leverage > self.max_leverage:
            raise GameError(f"倍数需在 1~{self.max_leverage:g} 之间")
        state = await self._ensure_game_locked(user_id, group_id, None)
        if state.ended:
            raise GameError("本局已经结束")
        if state.positions or state.orders:
            raise GameError("有持仓或到价单时不能改倍数，先平仓/撤单")
        await self.store.set_session_leverage(state.session.id, leverage, int(self._clock()))
        return await self._ensure_game_locked(user_id, group_id, None)

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._board_locks.setdefault(key, asyncio.Lock())

    def _lock_for_player(self, user_id: str, group_id: str | None) -> asyncio.Lock:
        key = f"{user_id}:{group_id or ''}"
        return self._user_locks.setdefault(key, asyncio.Lock())

    async def _ensure_board(self, province: Province) -> Season:
        lock = self._lock_for(BOARD_OWNER)
        async with lock:
            existing = await self.store.get_running_season_for_owner(BOARD_OWNER)
            if existing is not None:
                return existing

            raw_local = (await self.client.fetch_minutely_15(province)).value
            local = raw_local
            now = int(self._clock())
            now_key = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%dT%H:%M")
            local = prune_minutely(local, TOTAL_BARS, now_key)
            end = len(local.time) - 1
            for index, value in enumerate(local.time):
                if value > now_key:
                    end = max(0, index - 1)
                    break
            if end + 1 < TOTAL_BARS:
                raise GameError(f"行情历史不足：需要 {TOTAL_BARS} 根，当前只有 {end + 1} 根")
            start = end - TOTAL_BARS + 1
            seed = _stable_seed(BOARD_OWNER, str(now))
            start_time = local.time[start]
            end_time = local.time[end]
            initial_bar = TOTAL_BARS - 1
            board = Season(
                id=f"{BOARD_OWNER}:{uuid.uuid4().hex[:8]}",
                province=province.name,
                city=province.capital,
                seed=seed,
                start_bar=start,
                end_bar=start + TOTAL_BARS - 1,
                status=SeasonStatus.RUNNING,
                created_at=now,
                owner=BOARD_OWNER,
                group_id=None,
                current_bar=initial_bar,
                updated_at=now,
                start_time=start_time,
                end_time=end_time,
            )
            await self.store.add_season(board)
            await self.store.save_market_series(board.id, province.name, local)
            return board

    async def _ensure_player(
        self,
        board: Season,
        session: Session | None,
        user_id: str,
        group_id: str | None,
        province: Province,
    ) -> Session:
        existing = session if session is not None and session.season_id == board.id else None
        if existing is None:
            existing = await self.store.get_latest_session_on_board(user_id, board.id, group_id)
        if existing is not None:
            if existing.status is not SessionStatus.RUNNING:
                return existing
            if existing.province != province.name:
                await self.store.set_session_province(existing.id, province.name)
                existing = replace(existing, province=province.name)
            return existing

        now = int(self._clock())
        session = Session(
            id=uuid.uuid4().hex,
            user_id=user_id,
            group_id=group_id,
            season_id=board.id,
            principal=self.default_principal,
            leverage=INITIAL_LEVERAGE,
            current_bar=board.current_bar,
            status=SessionStatus.RUNNING,
            created_at=now,
            updated_at=now,
            province=province.name,
        )
        await self.store.add_session(session)
        await self.store.save_account(
            Account(
                session_id=session.id,
                cash=session.principal,
                equity=session.principal,
                stake=0.0,
            )
        )
        return session

    # ------------------------------------------------------------------
    # 共享盘时钟 + 玩家账户懒推进
    # ------------------------------------------------------------------
    async def _advance_board(self, board: Season) -> Season:
        target = TOTAL_BARS - 1
        if board.current_bar == target:
            return board
        now = int(self._clock())
        await self.store.update_season_clock(board.id, current_bar=target, updated_at=now)
        return replace(board, current_bar=target, updated_at=now)

    async def _apply_safety_net(
        self, session: Session, account: Account, now: int
    ) -> tuple[Session, float]:
        today_date = datetime.fromtimestamp(now, TZ).date()
        today = today_date.isoformat()
        entry = self.default_principal * self.safety_entry_ratio
        grant = self.default_principal * self.safety_grant_ratio
        target = self.default_principal * self.safety_target_ratio
        if not session.safety_active:
            if account.equity >= entry:
                return session, 0.0
            session = replace(session, safety_active=True, safety_date="")
            await self.store.set_session_safety(session.id, active=True, grant_date="")
        if session.safety_date == today:
            return session, 0.0
        if account.equity >= target:
            session = replace(session, safety_active=False)
            await self.store.set_session_safety(
                session.id, active=False, grant_date=session.safety_date
            )
            return session, 0.0
        if session.safety_date:
            try:
                last = date.fromisoformat(session.safety_date)
            except ValueError:
                last = today_date
            days = max(1, (today_date - last).days)
        else:
            days = 1
        needed = max(1, math.ceil((target - account.equity) / grant))
        grants = min(days, needed)
        amount = grants * grant
        account = replace(account, cash=account.cash + amount, equity=account.equity + amount)
        await self.store.save_account(account)
        active = account.equity < target
        session = replace(session, safety_active=active, safety_date=today)
        await self.store.set_session_safety(session.id, active=active, grant_date=today)
        return session, amount

    async def _market_bar(
        self, board: Season, province_name: str, relative: int, *, series: WxiSeries | None = None
    ) -> WxiBar | None:
        market = (
            series if series is not None else await self._load_wxi_for_name(board, province_name)
        )
        if market is None:
            return None
        index = min(relative, len(market) - 1)
        previous = market.bars[index - 1].temp if index > 0 else None
        return _display_bar(market.bars[index], previous)

    async def _equity_at(
        self,
        board: Season,
        positions: dict[str, Position],
        cash: float,
        relative: int,
        leverage: float,
        *,
        bar_at: Callable[[str, int], Awaitable[WxiBar | None]] | None = None,
    ) -> float:
        total = cash
        for position in positions.values():
            if bar_at is not None:
                bar = await bar_at(position.province, relative)
            else:
                bar = await self._market_bar(board, position.province, relative)
            price = bar.close if bar is not None else position.entry_price
            total += _bet_value(position, price, leverage)
        return total

    async def _backfill_series_map(
        self, board: Season, provinces: set[str], begin_ts: float, now_ts: float
    ) -> tuple[dict[str, WxiSeries], int, int]:
        """离线超窗口时，用最多 92 天历史构造回补序列，覆盖 (begin_ts, now_ts]。"""
        now_key = datetime.fromtimestamp(now_ts, TZ).strftime("%Y-%m-%dT%H:%M")
        begin_key = datetime.fromtimestamp(begin_ts, TZ).strftime("%Y-%m-%dT%H:%M")
        series_map: dict[str, WxiSeries] = {}
        start_rel = 0
        end_rel = 0
        window_start: int | None = None
        trade_days = TRADE_DAYS
        for name in sorted(provinces):
            province = resolve_province(name)
            if province is None:
                continue
            raw = (await self.client.fetch_minutely_15(province)).value
            local = raw
            begin_idx = 0
            for index, stamp in enumerate(local.time):
                if stamp > begin_key:
                    begin_idx = index
                    break
            end_idx = len(local.time) - 1
            for index, stamp in enumerate(local.time):
                if stamp > now_key:
                    end_idx = index - 1
                    break
            if end_idx < begin_idx:
                continue
            if window_start is None:
                window_start = max(0, begin_idx - WARM_DAYS * BARS_PER_DAY)
                needed = end_idx - window_start + 1
                trade_days = max(1, (needed + BARS_PER_DAY - 1) // BARS_PER_DAY)
                start_rel = begin_idx - window_start
                end_rel = end_idx - window_start
            if window_start + (WARM_DAYS + trade_days) * BARS_PER_DAY > len(local.time):
                trade_days = max(1, (len(local.time) - window_start) // BARS_PER_DAY - WARM_DAYS)
                if window_start + (WARM_DAYS + trade_days) * BARS_PER_DAY > len(local.time):
                    continue
            region = await self._market_region(
                board,
                province,
                local.time[window_start][:10],
                local.time[end_idx][:10],
            )
            series_map[name] = build_wxi_series(
                local,
                region,
                province_name=province.name,
                province_short=province.short,
                city_name=province.capital,
                city_id=province.city_id,
                seed=board.seed ^ _stable_seed(province.name),
                window_start=window_start,
                warm_days=WARM_DAYS,
                trade_days=trade_days,
            )
        return series_map, start_rel, end_rel

    async def _advance_player(self, session: Session, board: Season) -> Session:
        now = int(self._clock())
        elapsed = elapsed_bars(session.updated_at, now)
        if session.status is not SessionStatus.RUNNING or elapsed <= 0:
            return session
        account = await self.store.get_account(session.id)
        if account is None:
            account = Account(session.id, session.principal, session.principal, 0.0)
        positions = {
            position.province: position for position in await self.store.get_positions(session.id)
        }
        orders = list(await self.store.list_open_orders(session.id))

        start_rel = max(0, board.current_bar + 1 - elapsed)
        end_rel = board.current_bar
        series_map: dict[str, WxiSeries] = {}
        if elapsed > board.current_bar + 1:
            provinces = {board.province} | {order.province for order in orders} | set(positions)
            series_map, mapped_start, mapped_end = await self._backfill_series_map(
                board, provinces, session.updated_at, now
            )
            if series_map:
                start_rel, end_rel = mapped_start, mapped_end

        async def bar_at(province_name: str, relative: int) -> WxiBar | None:
            series = series_map.get(province_name)
            if series is not None:
                return await self._market_bar(board, province_name, relative, series=series)
            return await self._market_bar(board, province_name, relative)

        for relative in range(start_rel, end_rel + 1):
            opened_this_bar: set[str] = set()
            for index, order in enumerate(orders):
                if order.status is not OrderStatus.OPEN:
                    continue
                bar = await bar_at(order.province, relative)
                if bar is None or not _hit(order, bar):
                    continue
                if order.kind is OrderKind.BET:
                    if order.province in positions or account.cash + 1e-6 < order.stake:
                        continue
                    account = replace(
                        account,
                        cash=account.cash - order.stake,
                        stake=account.stake + order.stake,
                    )
                    position = Position(
                        session_id=session.id,
                        side=order.side,
                        stake=order.stake,
                        entry_price=order.price,
                        province=order.province,
                    )
                    positions[order.province] = position
                    opened_this_bar.add(order.province)
                    trade = Trade(
                        id=uuid.uuid4().hex,
                        session_id=session.id,
                        bar_index=relative,
                        side=order.side,
                        stake=order.stake,
                        price=order.price,
                        pnl=0.0,
                        created_at=now,
                        province=order.province,
                    )
                    async with self.store.transaction() as txn:
                        await txn.save_account(account)
                        await txn.save_position(position)
                        await txn.add_trade(trade)
                        await txn.set_order_status(order.id, OrderStatus.FILLED)
                else:
                    existing = positions.get(order.province)
                    if existing is None:
                        continue
                    account, _ = await self._close_value(
                        session,
                        existing,
                        order.price,
                        account,
                        positions,
                        board,
                        relative,
                        now,
                        order_id=order.id,
                    )
                orders[index] = replace(order, status=OrderStatus.FILLED)

            for province, position in list(positions.items()):
                if province in opened_this_bar:
                    continue
                bar = await bar_at(province, relative)
                if bar is None:
                    continue
                wipe = _wipe_price(position, session.leverage)
                hit = bar.low <= wipe if position.side is Side.LONG else bar.high >= wipe
                if not hit:
                    continue
                trade = Trade(
                    id=uuid.uuid4().hex,
                    session_id=session.id,
                    bar_index=relative,
                    side=position.side,
                    stake=position.stake,
                    price=wipe,
                    pnl=-position.stake,
                    created_at=now,
                    province=position.province,
                )
                remaining = {key: item for key, item in positions.items() if key != province}
                account = replace(
                    account,
                    stake=sum(item.stake for item in remaining.values()),
                    equity=account.cash,
                )
                async with self.store.transaction() as txn:
                    await txn.add_trade(trade)
                    await txn.delete_position(session.id, position.province)
                    await txn.save_account(account)
                positions.pop(province, None)

            account = replace(
                account,
                equity=await self._equity_at(
                    board, positions, account.cash, relative, session.leverage, bar_at=bar_at
                ),
                stake=sum(item.stake for item in positions.values()),
            )
            await self.store.save_account(account)

        await self.store.update_session(session.id, current_bar=board.current_bar, updated_at=now)
        return replace(session, current_bar=board.current_bar, updated_at=now)

    # ------------------------------------------------------------------
    # 盘内省份行情
    # ------------------------------------------------------------------
    async def _market_series(self, board: Season, province: Province):
        local = await self.store.load_market_series(board.id, province.name)
        now = int(self._clock())
        if local is not None and now - int(local.fetched_at) < REFRESH_SECONDS:
            return local
        raw = (await self.client.fetch_minutely_15(province)).value
        fresh = raw
        merged = merge_minutely(local, fresh) if local is not None else fresh
        now_key = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%dT%H:%M")
        pruned = prune_minutely(merged, TOTAL_BARS, now_key)
        await self.store.save_market_series(board.id, province.name, pruned)
        return pruned

    async def _market_region(
        self,
        board: Season,
        province: Province,
        start_date: str,
        end_date: str,
    ):
        if not self.region_enabled:
            return None
        # 区域序列按当前窗口日期拉取；store 旧值只在取数失败时兜底
        selection = region_cities(province)
        try:
            raw = (
                await self.client.fetch_region(
                    province,
                    selection,
                    start_date,
                    end_date,
                )
            ).value
            await self.store.save_market_region(board.id, province.name, raw)
            return raw
        except Exception as exc:  # noqa: BLE001 - 区域失败退化为本地行情
            logger.warning(f"区域指数取数失败，退化为本地行情：{exc}")
            return await self.store.load_market_region(board.id, province.name)

    async def _load_wxi_for_name(self, board: Season, province_name: str) -> WxiSeries | None:
        province = resolve_province(province_name)
        if province is None:
            return None
        return await self._load_wxi(board, province)

    async def _load_wxi(self, board: Season, province: Province) -> WxiSeries:
        local = await self._market_series(board, province)
        now = int(self._clock())
        now_key = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%dT%H:%M")
        end = len(local.time) - 1
        for index, stamp in enumerate(local.time):
            if stamp > now_key:
                end = max(0, index - 1)
                break
        if end + 1 < TOTAL_BARS:
            raise GameError(f"{province.short}的行情历史不足，无法建盘")
        start = end - TOTAL_BARS + 1
        key = (board.id, province.name, local.time[end])
        cached = self._wxi_cache.get(key)
        if cached is not None:
            self._wxi_cache.move_to_end(key)
            return cached
        region = await self._market_region(
            board,
            province,
            local.time[start][:10],
            local.time[end][:10],
        )
        forecast = None
        try:
            forecast = (await self.client.fetch_forecast_deviation(province)).value
        except Exception as exc:  # noqa: BLE001 - 预报偏离失败不影响主行情
            logger.warning(f"预报偏离取数失败，按 0 处理：{exc}")
        wxi = build_wxi_series(
            local,
            region,
            province_name=province.name,
            province_short=province.short,
            city_name=province.capital,
            city_id=province.city_id,
            seed=board.seed ^ _stable_seed(province.name),
            window_start=start,
            forecast=forecast,
        )
        self._wxi_cache[key] = wxi
        self._wxi_cache.move_to_end(key)
        while len(self._wxi_cache) > 16:
            self._wxi_cache.popitem(last=False)
        return wxi

    async def _state_for(
        self,
        session: Session,
        board: Season,
        province: Province,
        *,
        safety_grant: float = 0.0,
    ) -> GameState:
        wxi = await self._load_wxi(board, province)
        account = await self.store.get_account(session.id)
        if account is None:
            account = Account(session.id, session.principal, session.principal, 0.0)
        positions = tuple(await self.store.get_positions(session.id))
        orders = tuple(await self.store.list_open_orders(session.id))
        prices: list[tuple[str, float]] = []
        for position in positions:
            bar = await self._market_bar(board, position.province, board.current_bar)
            if bar is not None:
                prices.append((position.province, bar.close))
        return GameState(
            session=session,
            season=board,
            wxi=wxi,
            account=account,
            positions=positions,
            orders=orders,
            position_prices=tuple(prices),
            safety_grant=safety_grant,
            ended=session.status is not SessionStatus.RUNNING,
        )

    async def _existing_state(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> GameState | None:
        board = await self.store.get_running_season_for_owner(BOARD_OWNER)
        if board is None:
            return None
        session = await self.store.get_latest_session_on_board(user_id, board.id, group_id)
        selected = province
        if selected is None and session is not None:
            selected = resolve_province(session.province)
        if selected is None:
            selected = resolve_province(board.province)
        if selected is None:
            raise GameError("请指定省份")
        if session is None:
            session = await self._ensure_player(board, None, user_id, group_id, selected)
        if session.status is not SessionStatus.RUNNING:
            raise GameError("你的这一盘已经结束；等下一个盘再玩")
        if session.province != selected.name:
            await self.store.set_session_province(session.id, selected.name)
            session = replace(session, province=selected.name)
        board = await self._advance_board(board)
        session = await self._advance_player(session, board)
        account = await self.store.get_account(session.id)
        if account is None:
            account = Account(session.id, session.principal, session.principal, 0.0)
        session, safety_grant = await self._apply_safety_net(session, account, int(self._clock()))
        return await self._state_for(session, board, selected, safety_grant=safety_grant)

    # ------------------------------------------------------------------
    # 玩家操作
    # ------------------------------------------------------------------
    @_player_locked
    async def open_position(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None,
        side: Side,
        percent: float | None = None,
        *,
        stake: float | None = None,
    ) -> TradeOutcome:
        state = await self._ensure_game_locked(user_id, group_id, province)
        if state.ended:
            raise GameError("本局已经结束")
        selected = state.wxi.province_name
        if any(position.province == selected for position in state.positions):
            short = state.wxi.province_short
            raise GameError(f"{short}已经有持仓，先「平仓 {short}」再买")
        cash = state.account.cash
        if cash < MIN_STAKE:
            raise GameError(f"余额不足 {MIN_STAKE:.0f}，不能买入")
        if stake is None:
            ratio = 30.0 if percent is None else max(1.0, min(100.0, float(percent)))
            stake = cash * ratio / 100.0
        stake = float(stake)
        if not math.isfinite(stake):
            raise GameError("金额格式不对")
        if stake < MIN_STAKE:
            raise GameError(f"最少投入 {MIN_STAKE:.0f}")
        if stake > cash + 1e-6:
            raise GameError(f"余额只有 {cash:,.0f}")
        stake = min(stake, cash)
        price = state.current.close
        if not math.isfinite(price) or price <= 0:
            raise GameError("行情异常，稍后再试")
        account = replace(
            state.account,
            cash=cash - stake,
            equity=state.account.equity,
            stake=state.account.stake + stake,
        )
        position = Position(
            session_id=state.session.id,
            side=side,
            stake=stake,
            entry_price=price,
            province=selected,
        )
        trade = Trade(
            id=uuid.uuid4().hex,
            session_id=state.session.id,
            bar_index=state.season.current_bar,
            side=side,
            stake=stake,
            price=price,
            pnl=0.0,
            created_at=int(self._clock()),
            province=selected,
        )
        async with self.store.transaction() as txn:
            await txn.save_account(account)
            await txn.save_position(position)
            await txn.add_trade(trade)
        return TradeOutcome(
            state=replace(state, account=account, positions=state.positions + (position,)),
            trade=trade,
        )

    @_player_locked
    async def close_position(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> CloseOutcome:
        state = await self._existing_state(user_id, group_id, province)
        if state is None:
            return CloseOutcome(state=None, trades=(), total_pnl=0.0)
        positions = {position.province: position for position in state.positions}
        if province is not None:
            position = positions.get(province.name)
            if position is None:
                raise GameError(f"{province.short}没有持仓")
            targets = [position]
        else:
            targets = list(positions.values())
        if not targets:
            return CloseOutcome(state=state, trades=(), total_pnl=0.0)

        account = state.account
        trades: list[Trade] = []
        for position in targets:
            account, trade = await self._close_value(
                state.session,
                position,
                state.price_for(position),
                account,
                positions,
                state.season,
                state.season.current_bar,
                int(self._clock()),
            )
            trades.append(trade)
        return CloseOutcome(
            state=replace(state, account=account, positions=tuple(positions.values())),
            trades=tuple(trades),
            total_pnl=sum(trade.pnl for trade in trades),
        )

    @_player_locked
    async def place_order(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None,
        kind: OrderKind,
        side: Side | None,
        price: float,
        percent: float = 30.0,
        *,
        stake: float | None = None,
    ) -> OrderOutcome:
        if kind is OrderKind.BET:
            state = await self._ensure_game_locked(user_id, group_id, province)
        else:
            existing = await self._existing_state(user_id, group_id, province)
            if existing is None:
                raise GameError("当前没有持仓，不能挂到价平仓")
            state = existing
        if state.ended:
            raise GameError("本局已经结束")
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            raise GameError("到价价格必须大于 0")
        now = int(self._clock())
        if kind is OrderKind.BET:
            if side is None:
                raise GameError("到价下单需要选择买入或卖出")
            selected = state.wxi.province_name
            if any(
                order.kind is OrderKind.BET and order.province == selected for order in state.orders
            ):
                raise GameError("这个省已经有一个到价买入；先「撤单」再挂新的")
            if any(position.province == selected for position in state.positions):
                raise GameError("这个省已经有持仓，先平仓再挂到价买入")
            cash = state.account.cash
            if stake is None:
                ratio = max(1.0, min(100.0, float(percent)))
                stake = cash * ratio / 100.0
            stake = float(stake)
            if not math.isfinite(stake):
                raise GameError("金额格式不对")
            if stake < MIN_STAKE:
                raise GameError(f"最少投入 {MIN_STAKE:.0f}")
            if stake > cash + 1e-6:
                raise GameError(f"余额只有 {cash:,.0f}")
            order = Order(
                id=uuid.uuid4().hex,
                session_id=state.session.id,
                kind=OrderKind.BET,
                side=side,
                stake=stake,
                price=price,
                status=OrderStatus.OPEN,
                created_at=now,
                province=selected,
            )
        else:
            if province is not None:
                position = next(
                    (item for item in state.positions if item.province == province.name),
                    None,
                )
                if position is None:
                    raise GameError(f"{province.short}没有持仓")
            elif len(state.positions) == 1:
                position = state.positions[0]
            elif len(state.positions) > 1:
                raise GameError("持仓有多个省，请指定：到价 河北 1050 平")
            else:
                raise GameError("当前没有持仓，不能挂到价平仓")
            if any(
                order.kind is OrderKind.CLOSE and order.province == position.province
                for order in state.orders
            ):
                raise GameError("这个省已经有一个到价平仓；先「撤单」再挂新的")
            order = Order(
                id=uuid.uuid4().hex,
                session_id=state.session.id,
                kind=OrderKind.CLOSE,
                side=position.side,
                stake=0.0,
                price=price,
                status=OrderStatus.OPEN,
                created_at=now,
                province=position.province,
            )
        await self.store.add_order(order)
        return OrderOutcome(state=replace(state, orders=state.orders + (order,)), order=order)

    @_player_locked
    async def cancel_orders(self, user_id: str, group_id: str | None) -> int:
        state = await self._existing_state(user_id, group_id)
        if state is None:
            return 0
        return await self.store.cancel_open_orders(state.session.id)

    # ------------------------------------------------------------------
    # 平仓辅助
    # ------------------------------------------------------------------
    async def _close_value(
        self,
        session: Session,
        position: Position,
        price: float,
        account: Account,
        positions: dict[str, Position],
        board: Season,
        relative: int,
        now: int,
        *,
        order_id: str | None = None,
    ) -> tuple[Account, Trade]:
        value = _bet_value(position, price, session.leverage)
        trade = Trade(
            id=uuid.uuid4().hex,
            session_id=session.id,
            bar_index=relative,
            side=position.side,
            stake=position.stake,
            price=price,
            pnl=value - position.stake,
            created_at=now,
            province=position.province,
        )
        remaining = {key: item for key, item in positions.items() if key != position.province}
        cash = account.cash + value
        stake = sum(item.stake for item in remaining.values())
        equity = await self._equity_at(board, remaining, cash, relative, session.leverage)
        next_account = replace(account, cash=cash, equity=equity, stake=stake)
        async with self.store.transaction() as txn:
            await txn.add_trade(trade)
            await txn.delete_position(session.id, position.province)
            await txn.save_account(next_account)
            if order_id is not None:
                await txn.set_order_status(order_id, OrderStatus.FILLED)
        positions.pop(position.province, None)
        return next_account, trade

    # ------------------------------------------------------------------
    # 行情/报价头/图表
    # ------------------------------------------------------------------
    def quote_stats(self, state: GameState) -> QuoteStats:
        relative = state.relative_bar
        bars = state.wxi.bars
        bar = _display_bar(bars[relative], bars[relative - 1].temp if relative > 0 else None)
        previous_temp = bars[relative - 1].temp if relative > 0 else bar.temp
        change = bar.temp - previous_temp
        change_pct = change / previous_temp * 100 if previous_temp else 0.0
        closes = [item.temp for item in bars[: relative + 1]]
        ma5 = _moving_average(closes, 5)[-1]
        ma20 = _moving_average(closes, 20)[-1]
        regional = bar.regional
        previous_regional: float | None = None
        if relative > 0:
            previous_bar = _display_bar(
                bars[relative - 1],
                bars[relative - 2].temp if relative > 1 else None,
            )
            previous_regional = previous_bar.regional
        regional_change: float | None = None
        if regional is not None and previous_regional is not None:
            regional_change = regional - previous_regional
        today = bar.time[:10]
        day_start = relative
        while day_start > 0 and bars[day_start - 1].time[:10] == today:
            day_start -= 1
        day_bars = bars[day_start : relative + 1]
        temp_high = max(item.temp for item in day_bars)
        temp_low = min(item.temp for item in day_bars)
        day_open_temp = day_bars[0].temp
        previous_day = bars[day_start - 1].time[:10] if day_start > 0 else None
        prev_day_bars = [item for item in bars[:day_start] if item.time[:10] == previous_day]
        day = relative // BARS_PER_DAY + 1
        prev_day_high = max((item.temp for item in prev_day_bars), default=None)
        prev_day_low = min((item.temp for item in prev_day_bars), default=None)
        prev_day_close_temp = bars[day_start - 1].temp if day_start > 0 else None
        humidity = _humidity(bar.temp, bar.dew)
        feels_like = _feels_like(bar.temp, humidity, bar.gust * 0.7)
        moment = datetime.fromisoformat(bar.time)
        time_label = f"{moment:%m-%d} {moment:%H:%M}"
        session_label = "早盘" if moment.hour < 12 else "午盘"
        status, is_open = market_status()
        return QuoteStats(
            price=bar.temp,
            change=change,
            change_pct=change_pct,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            ma5=ma5,
            ma20=ma20,
            activity=bar.activity,
            regional=regional,
            regional_change=regional_change,
            day=day,
            time_label=time_label,
            session_label=session_label,
            day_open=day_open_temp,
            prev_close=prev_day_close_temp,
            day_high=temp_high,
            day_low=temp_low,
            amplitude=temp_high - temp_low,
            market_status=status,
            market_open=is_open,
            temp_high=temp_high,
            temp_low=temp_low,
            temp_amplitude=temp_high - temp_low,
            feels_like=feels_like,
            humidity=humidity,
            day_open_temp=day_open_temp,
            prev_day_close_temp=prev_day_close_temp,
            prev_day_high=prev_day_high,
            prev_day_low=prev_day_low,
            observations=len(state.wxi.region_cities),
        )

    def comfort_pressure(self, state: GameState) -> tuple[dict[str, Any], ...]:
        """天气压力表：六个维度近 7 个交易日的分位、方向与贡献点。"""
        relative = state.relative_bar
        bars = state.wxi.bars
        start = max(0, relative - 7 * BARS_PER_DAY + 1)
        window = bars[start : relative + 1]
        current = bars[relative]
        previous = bars[relative - 1] if relative > 0 else None
        current_scores = bar_scores(
            current.temp, current.dew, current.precip, current.wcode, current.gust
        )
        window_scores = [
            bar_scores(item.temp, item.dew, item.precip, item.wcode, item.gust) for item in window
        ]
        previous_scores = (
            bar_scores(previous.temp, previous.dew, previous.precip, previous.wcode, previous.gust)
            if previous is not None
            else current_scores
        )
        labels = {
            "temp": "气温",
            "dew": "露点",
            "precip": "降水",
            "code": "天气",
            "gust": "阵风",
            "air": "空气",
        }
        rows: list[dict[str, Any]] = []
        for key, weight in COMFORT_WEIGHTS.items():
            score = current_scores[key]
            series = [item[key] for item in window_scores]
            percentile = 100.0 * sum(1 for value in series if value <= score) / len(series)
            delta = score - previous_scores[key]
            arrow = "升" if delta > 0.5 else "降" if delta < -0.5 else "平"
            contribution = weight * (score - 100.0)
            if key == "air":
                # 原版没有 PM2.5，空气项固定 85 分，没有分位可言
                text = f"固定 {contribution:+.1f}"
            else:
                text = f"{percentile:.0f}% {arrow} {contribution:+.1f}"
            rows.append(
                {
                    "key": key,
                    "label": labels[key],
                    "percentile": percentile,
                    "direction": arrow,
                    "contribution": contribution,
                    "dominant": False,
                    "text": text,
                }
            )
        # 空气是占位值，恒定 85 分，不能让它抢「主导」
        worst = min(
            (row for row in rows if row["key"] != "air"),
            key=lambda row: float(row["contribution"]),
        )
        worst["dominant"] = True
        return tuple(rows)

    def quote_head(self, state: GameState, stats: QuoteStats) -> dict[str, object]:
        bar = state.current

        def num(value: float | None, digits: int = 1) -> str:
            return "—" if value is None else f"{value:,.{digits}f}"

        def temp(value: float | None) -> str:
            return "—" if value is None else f"{value:.1f}℃"

        groups = (
            (
                ("今日最高", temp(stats.temp_high), "up"),
                ("今日最低", temp(stats.temp_low), "down"),
                (
                    "体感",
                    "—" if stats.feels_like is None else f"{stats.feels_like:.1f}℃",
                ),
                ("全天波动", f"{stats.temp_amplitude:.1f}℃"),
            ),
            (
                ("阵风", f"{bar.gust:.1f} m/s {consts.wind_level(bar.gust)}级"),
                ("降水", f"{bar.precip:.1f} mm"),
                (
                    "湿度",
                    "—" if stats.humidity is None else f"{stats.humidity:.0f}%",
                ),
                ("露点", "—" if bar.dew is None else f"{bar.dew:.1f}℃"),
            ),
            (
                ("舒适度", f"{bar.comfort:.0f}"),
                ("天气", consts.weather_name(bar.wcode)),
                ("CAPE", f"{bar.cape}"),
                ("活跃度", f"{bar.activity:.1f}"),
                (
                    "预报偏离",
                    "—" if bar.forecast_dev is None else f"{bar.forecast_dev:+.1f}℃",
                ),
            ),
            (
                ("MA5", num(stats.ma5)),
                ("MA20", num(stats.ma20)),
                ("区域", num(stats.regional)),
                (
                    "区域涨跌",
                    "—" if stats.regional_change is None else f"{stats.regional_change:+.1f}℃",
                ),
            ),
            (
                ("今开", num(stats.day_open_temp)),
                ("昨收", num(stats.prev_day_close_temp)),
                (
                    "昨高/低",
                    f"{num(stats.prev_day_high)}/{num(stats.prev_day_low)}",
                ),
                ("观测点", f"{stats.observations} 个"),
            ),
        )
        pressure = self.comfort_pressure(state)
        return {
            "title": f"{state.wxi.province_short} · {state.wxi.city_name}",
            "subtitle": f"{state.wxi.city_id} · {state.wxi.province_name}",
            "tags": (
                consts.weather_name(bar.wcode),
                stats.time_label,
                stats.market_status,
            ),
            "price": f"{stats.price:.1f}",
            "change": f"{stats.change:+.1f}",
            "change_pct": f"{stats.change_pct:+.2f}%",
            "up": stats.change >= 0,
            "pressure": pressure,
            "groups": groups,
        }

    async def chart_forecast(self, state: GameState) -> tuple[Bar, ...]:
        province = resolve_province(state.wxi.province_name)
        if province is None:
            return ()
        try:
            raw = (await self.client.fetch_minutely_15(province)).value
        except Exception as exc:  # noqa: BLE001 - 预测尾段失败时只画历史
            logger.warning(f"主图预测尾段取数失败：{exc}")
            return ()
        local = raw
        now_key = datetime.fromtimestamp(self._clock(), TZ).strftime("%Y-%m-%dT%H:%M")
        count = max(1, round(self.chart_bars * 0.15))
        points: list[tuple[str, float]] = []
        for time_value, temp_value in zip(local.time, local.temp, strict=True):
            if time_value <= now_key or temp_value is None:
                continue
            points.append((time_value, float(temp_value)))
            if len(points) >= count:
                break
        previous = state.current.close
        bars: list[Bar] = []
        for stamp, temp in points:
            close = temp
            wick = max(0.05, abs(close - previous) * 0.15 + 0.02)
            bars.append(
                Bar(
                    time=f"{stamp[5:10]} {stamp[11:16]}",
                    open=previous,
                    high=max(previous, close) + wick,
                    low=min(previous, close) - wick,
                    close=close,
                    volume=0.0,
                )
            )
            previous = close
        return tuple(bars)

    def chart_data(self, state: GameState, forecast: tuple[Bar, ...] = ()) -> ChartData:
        relative = state.relative_bar
        past_count = max(1, self.chart_bars - len(forecast))
        start = max(0, relative - past_count + 1)
        closes = [item.temp for item in state.wxi.bars[: relative + 1]]
        ma5 = _moving_average(closes, 5)[start : relative + 1]
        ma20 = _moving_average(closes, 20)[start : relative + 1]
        bars: list[Bar] = []
        regional: list[float | None] = []
        for index in range(start, relative + 1):
            previous = state.wxi.bars[index - 1].temp if index > 0 else None
            item = _display_bar(state.wxi.bars[index], previous)
            bars.append(
                Bar(
                    time=f"{item.time[5:10]} {item.time[11:16]}",
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.activity,
                )
            )
            regional.append(item.regional)
        return ChartData(
            bars=tuple(bars),
            regional=tuple(regional),
            ma5=tuple(ma5),
            ma20=tuple(ma20),
            current_price=state.current.close,
            forecast=forecast,
        )


_engine: GameEngine | None = None


def get_engine() -> GameEngine:
    global _engine
    if _engine is None:
        from nonebot import get_plugin_config

        from ..config import Config
        from ..services.clock import get_clock
        from ..services.openmeteo import get_client
        from ..services.store import get_store

        config = get_plugin_config(Config)
        _engine = GameEngine(
            get_store(),
            get_client(),
            default_principal=config.weather_fighter_principal,
            max_leverage=config.weather_fighter_leverage,
            safety_entry_ratio=config.weather_fighter_safety_entry_pct / 100.0,
            safety_grant_ratio=config.weather_fighter_safety_grant_pct / 100.0,
            safety_target_ratio=config.weather_fighter_safety_target_pct / 100.0,
            chart_bars=config.weather_fighter_chart_bars,
            region_enabled=config.weather_fighter_enable_region,
            clock=get_clock().now,
        )
    return _engine
