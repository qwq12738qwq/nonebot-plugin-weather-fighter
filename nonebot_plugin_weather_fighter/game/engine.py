from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

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
    is_trading_day,
    market_status,
    trading_bar_of_day,
)
from ..services.openmeteo import OpenMeteoClient, filter_trading, filter_trading_region
from ..services.provinces import Province, resolve_province
from ..services.regions import region_cities
from ..services.store import Store
from .wxi import (
    TRADE_DAYS,
    WARM_DAYS,
    WxiBar,
    WxiSeries,
    build_wxi_series,
    extend_wxi,
    pick_window_start,
)

DEFAULT_PRINCIPAL = 10_000.0
DEFAULT_LEVERAGE = 10.0
MIN_STAKE = 100.0
DEFAULT_SAFETY_ENTRY = 0.20
DEFAULT_SAFETY_GRANT = 0.05
DEFAULT_SAFETY_TARGET = 0.35
DISPLAY_WICK_FACTOR = 0.8
DISPLAY_REGIONAL_FACTOR = 0.02
CHART_BARS = 240
TOTAL_BARS = (WARM_DAYS + TRADE_DAYS) * BARS_PER_DAY
HORIZON_CYCLES = 50


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
    regional_change_pct: float | None
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
    regional = None
    if bar.regional is not None:
        regional = close + (bar.regional - bar.close) * DISPLAY_REGIONAL_FACTOR
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


def _owner_key(user_id: str, group_id: str | None) -> str:
    return f"g:{group_id}" if group_id is not None else f"u:{user_id}"


def _stable_seed(*parts: str) -> int:
    payload = "|".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFFFFFF_FFFF


def _first_trade_day(created_at: float) -> date:
    day = datetime.fromtimestamp(created_at, TZ).date()
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def _trading_day_count(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    day = start
    while day <= end:
        if is_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count


def _board_current_bar(created_at: float, now: float) -> int:
    first = _first_trade_day(created_at)
    today = datetime.fromtimestamp(now, TZ).date()
    if today < first:
        return WARM_DAYS * BARS_PER_DAY
    offset = max(0, _trading_day_count(first, today) - 1)
    return WARM_DAYS * BARS_PER_DAY + offset * BARS_PER_DAY + trading_bar_of_day(now)


class GameEngine:
    def __init__(
        self,
        store: Store,
        client: OpenMeteoClient,
        *,
        default_principal: float = DEFAULT_PRINCIPAL,
        default_leverage: float = DEFAULT_LEVERAGE,
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
        self.default_leverage = float(default_leverage)
        self.safety_entry_ratio = float(safety_entry_ratio)
        self.safety_grant_ratio = float(safety_grant_ratio)
        self.safety_target_ratio = float(safety_target_ratio)
        self.chart_bars = chart_bars
        self.region_enabled = region_enabled
        self._clock = clock
        self._wxi_cache: OrderedDict[tuple[str, str], WxiSeries] = OrderedDict()
        self._board_locks: dict[str, asyncio.Lock] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 开局：一个群一个盘；玩家只是这个盘里的一个账户
    # ------------------------------------------------------------------
    async def ensure_game(
        self,
        user_id: str,
        group_id: str | None,
        province: Province | None = None,
    ) -> GameState:
        owner = _owner_key(user_id, group_id)
        board = await self.store.get_running_season_for_owner(owner)
        session = await self.store.get_latest_running_session_for_owner(user_id, owner)

        selected = province
        if selected is None and session is not None:
            selected = resolve_province(session.province)
        if selected is None and board is not None:
            selected = resolve_province(board.province)
        if selected is None:
            raise GameError("请指定省份，例如：行情 河北 / 买入 河北 1000")

        if board is None:
            board = await self._ensure_board(owner, group_id, selected)
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

    async def last_province(self, user_id: str, group_id: str | None) -> Province | None:
        owner = _owner_key(user_id, group_id)
        session = await self.store.get_latest_running_session_for_owner(user_id, owner)
        if session is None or not session.province:
            return None
        return resolve_province(session.province)

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._board_locks.setdefault(key, asyncio.Lock())

    async def _ensure_board(self, owner: str, group_id: str | None, province: Province) -> Season:
        lock = self._lock_for(owner)
        async with lock:
            existing = await self.store.get_running_season_for_owner(owner)
            if existing is not None:
                return existing

            raw_local = (await self.client.fetch_minutely_15(province)).value
            local = filter_trading(raw_local)
            seed = _stable_seed(owner, str(int(self._clock())))
            start = pick_window_start(local.time, seed)
            start_time = local.time[start]
            end_time = local.time[start + TOTAL_BARS - 1]
            now = int(self._clock())
            initial_bar = _board_current_bar(now, now)
            board = Season(
                id=f"{owner}:{uuid.uuid4().hex[:8]}",
                province=province.name,
                city=province.capital,
                seed=seed,
                start_bar=start,
                end_bar=start + TOTAL_BARS - 1,
                status=SeasonStatus.RUNNING,
                created_at=now,
                owner=owner,
                group_id=group_id,
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
            existing = await self.store.get_latest_session_on_board(user_id, board.id)
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
            leverage=self.default_leverage,
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
        now = int(self._clock())
        target = _board_current_bar(board.created_at, now)
        if target <= board.current_bar:
            return board
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

    async def _market_bar(self, board: Season, province_name: str, relative: int) -> WxiBar | None:
        market = await self._load_wxi_for_name(board, province_name)
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
    ) -> float:
        total = cash
        for position in positions.values():
            bar = await self._market_bar(board, position.province, relative)
            price = bar.close if bar is not None else position.entry_price
            total += _bet_value(position, price, leverage)
        return total

    async def _advance_player(self, session: Session, board: Season) -> Session:
        if session.status is not SessionStatus.RUNNING or session.current_bar >= board.current_bar:
            return session
        account = await self.store.get_account(session.id)
        if account is None:
            account = Account(session.id, session.principal, session.principal, 0.0)
        positions = {
            position.province: position for position in await self.store.get_positions(session.id)
        }
        orders = list(await self.store.list_open_orders(session.id))
        now = int(self._clock())

        for relative in range(session.current_bar + 1, board.current_bar + 1):
            opened_this_bar: set[str] = set()
            for index, order in enumerate(orders):
                if order.status is not OrderStatus.OPEN:
                    continue
                bar = await self._market_bar(board, order.province, relative)
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
                    await self.store.add_trade(
                        Trade(
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
                    )
                    await self.store.save_position(position)
                    await self.store.save_account(account)
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
                    )
                await self.store.set_order_status(order.id, OrderStatus.FILLED)
                orders[index] = replace(order, status=OrderStatus.FILLED)

            for province, position in list(positions.items()):
                if province in opened_this_bar:
                    continue
                bar = await self._market_bar(board, province, relative)
                if bar is None:
                    continue
                wipe = _wipe_price(position, session.leverage)
                hit = bar.low <= wipe if position.side is Side.LONG else bar.high >= wipe
                if not hit:
                    continue
                await self.store.add_trade(
                    Trade(
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
                )
                await self.store.delete_position(session.id, position.province)
                positions.pop(province, None)
                account = replace(
                    account,
                    stake=sum(item.stake for item in positions.values()),
                    equity=account.cash,
                )
                await self.store.save_account(account)

            account = replace(
                account,
                equity=await self._equity_at(
                    board, positions, account.cash, relative, session.leverage
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
        if local is not None:
            return local
        raw = (await self.client.fetch_minutely_15(province)).value
        local = filter_trading(raw)
        await self.store.save_market_series(board.id, province.name, local)
        return local

    async def _market_region(self, board: Season, province: Province):
        region = await self.store.load_market_region(board.id, province.name)
        if region is not None:
            return region
        selection = region_cities(province)
        try:
            raw = (
                await self.client.fetch_region(
                    province,
                    selection,
                    board.start_time[:10],
                    board.end_time[:10],
                )
            ).value
            region = filter_trading_region(raw)
            await self.store.save_market_region(board.id, province.name, region)
            return region
        except Exception as exc:  # noqa: BLE001 - 区域失败退化为本地行情
            logger.warning(f"区域指数取数失败，退化为本地行情：{exc}")
            return None

    async def _load_wxi_for_name(self, board: Season, province_name: str) -> WxiSeries | None:
        province = resolve_province(province_name)
        if province is None:
            return None
        return await self._load_wxi(board, province)

    async def _load_wxi(self, board: Season, province: Province) -> WxiSeries:
        key = (board.id, province.name)
        cycle_bars = TRADE_DAYS * BARS_PER_DAY
        cached = self._wxi_cache.get(key)
        if cached is not None and board.current_bar < len(cached) - cycle_bars:
            self._wxi_cache.move_to_end(key)
            return cached
        cycles = HORIZON_CYCLES
        if cached is not None:
            cycles = max(HORIZON_CYCLES, (cached.trade_bars // cycle_bars) * 2)
        local = await self._market_series(board, province)
        region = await self._market_region(board, province)
        try:
            start = local.time.index(board.start_time)
        except ValueError as exc:
            raise GameError(f"{province.short}的行情时间轴与当前盘未对齐") from exc
        if (
            start + TOTAL_BARS > len(local.time)
            or local.time[start + TOTAL_BARS - 1] != board.end_time
        ):
            raise GameError(f"{province.short}的行情时间轴与当前盘未对齐")
        wxi = extend_wxi(
            build_wxi_series(
                local,
                region,
                province_name=province.name,
                province_short=province.short,
                city_name=province.capital,
                city_id=province.city_id,
                seed=board.seed ^ _stable_seed(province.name),
                window_start=start,
            ),
            cycles,
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
        owner = _owner_key(user_id, group_id)
        board = await self.store.get_running_season_for_owner(owner)
        if board is None:
            return None
        session = await self.store.get_latest_session_on_board(user_id, board.id)
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
            raise GameError("你的这一盘已经结束；等下一个群盘再玩")
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
        state = await self.ensure_game(user_id, group_id, province)
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
        if stake < MIN_STAKE:
            raise GameError(f"最少投入 {MIN_STAKE:.0f}")
        if stake > cash + 1e-6:
            raise GameError(f"余额只有 {cash:,.0f}")
        stake = min(stake, cash)
        price = state.current.close
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
        await self.store.save_account(account)
        await self.store.save_position(position)
        await self.store.add_trade(trade)
        return TradeOutcome(
            state=replace(state, account=account, positions=state.positions + (position,)),
            trade=trade,
        )

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
            state = await self.ensure_game(user_id, group_id, province)
        else:
            existing = await self._existing_state(user_id, group_id, province)
            if existing is None:
                raise GameError("当前没有持仓，不能挂到价平仓")
            state = existing
        if state.ended:
            raise GameError("本局已经结束")
        price = float(price)
        if price <= 0:
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
        await self.store.add_trade(trade)
        await self.store.delete_position(session.id, position.province)
        positions.pop(position.province, None)
        cash = account.cash + value
        stake = sum(item.stake for item in positions.values())
        equity = await self._equity_at(board, positions, cash, relative, session.leverage)
        next_account = replace(account, cash=cash, equity=equity, stake=stake)
        await self.store.save_account(next_account)
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
        regional_change_pct: float | None = None
        if regional is not None and previous_regional is not None and previous_regional != 0:
            regional_change_pct = (regional - previous_regional) / previous_regional * 100
        day = max(1, relative // BARS_PER_DAY + 1 - WARM_DAYS)
        day_start = (relative // BARS_PER_DAY) * BARS_PER_DAY
        day_bars = bars[day_start : relative + 1]
        temp_high = max(item.temp for item in day_bars)
        temp_low = min(item.temp for item in day_bars)
        day_open_temp = day_bars[0].temp
        prev_day_start = day_start - BARS_PER_DAY
        prev_day_bars = bars[prev_day_start:day_start] if prev_day_start >= 0 else []
        prev_day_high = max((item.temp for item in prev_day_bars), default=None)
        prev_day_low = min((item.temp for item in prev_day_bars), default=None)
        prev_day_close_temp = bars[day_start - 1].temp if day_start > 0 else None
        humidity = _humidity(bar.temp, bar.dew)
        feels_like = _feels_like(bar.temp, humidity, bar.gust * 0.7)
        moment = datetime.fromisoformat(bar.time)
        time_label = f"第 {day} 天 {moment:%H:%M}"
        session_label = "早盘" if moment.hour < 12 else "午盘"
        status, is_open = market_status(self._clock())
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
            regional_change_pct=regional_change_pct,
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

    def quote_head(self, state: GameState, stats: QuoteStats) -> dict[str, object]:
        bar = state.current

        def num(value: float | None, digits: int = 1) -> str:
            return "—" if value is None else f"{value:,.{digits}f}"

        groups = (
            (
                ("今日最高", num(stats.temp_high)),
                ("今日最低", num(stats.temp_low)),
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
            ),
            (
                ("MA5", num(stats.ma5)),
                ("MA20", num(stats.ma20)),
                ("区域", num(stats.regional)),
                (
                    "区域涨跌",
                    "—"
                    if stats.regional_change_pct is None
                    else f"{stats.regional_change_pct:+.2f}%",
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
            "groups": groups,
        }

    def chart_data(self, state: GameState) -> ChartData:
        relative = state.relative_bar
        start = max(0, relative - self.chart_bars + 1)
        closes = [item.temp for item in state.wxi.bars[: relative + 1]]
        ma5 = _moving_average(closes, 5)[start : relative + 1]
        ma20 = _moving_average(closes, 20)[start : relative + 1]
        bars: list[Bar] = []
        regional: list[float | None] = []
        for index in range(start, relative + 1):
            previous = state.wxi.bars[index - 1].temp if index > 0 else None
            item = _display_bar(state.wxi.bars[index], previous)
            day = max(1, index // BARS_PER_DAY + 1 - WARM_DAYS)
            bars.append(
                Bar(
                    time=f"{day}日 {item.time[11:16]}",
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
        )


_engine: GameEngine | None = None


def get_engine() -> GameEngine:
    global _engine
    if _engine is None:
        from nonebot import get_plugin_config

        from ..config import Config
        from ..services.openmeteo import get_client
        from ..services.store import get_store

        config = get_plugin_config(Config)
        _engine = GameEngine(
            get_store(),
            get_client(),
            default_principal=config.weather_fighter_principal,
            default_leverage=config.weather_fighter_leverage,
            safety_entry_ratio=config.weather_fighter_safety_entry_pct / 100.0,
            safety_grant_ratio=config.weather_fighter_safety_grant_pct / 100.0,
            safety_target_ratio=config.weather_fighter_safety_target_pct / 100.0,
            chart_bars=config.weather_fighter_chart_bars,
            region_enabled=config.weather_fighter_enable_region,
        )
    return _engine
