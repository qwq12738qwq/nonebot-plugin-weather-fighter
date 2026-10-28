from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class SeasonStatus(str, Enum):
    RUNNING = "running"
    SETTLED = "settled"


class SessionStatus(str, Enum):
    RUNNING = "running"
    SETTLED = "settled"
    LIQUIDATED = "liquidated"


class OrderKind(str, Enum):
    BET = "bet"
    CLOSE = "close"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Season:
    id: str
    province: str
    city: str
    seed: int
    start_bar: int
    end_bar: int
    status: SeasonStatus
    created_at: int
    owner: str = ""
    group_id: str | None = None
    current_bar: int = 0
    updated_at: int = 0
    speed: float = 1.0
    start_time: str = ""
    end_time: str = ""


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    user_id: str
    group_id: str | None
    season_id: str
    principal: float
    leverage: float
    current_bar: int
    status: SessionStatus
    created_at: int
    updated_at: int
    speed: float = 1.0
    return_pct: float | None = None
    province: str = ""
    safety_active: bool = False
    safety_date: str = ""


@dataclass(frozen=True, slots=True)
class Account:
    session_id: str
    cash: float
    equity: float
    stake: float


@dataclass(frozen=True, slots=True)
class Position:
    session_id: str
    side: Side
    stake: float
    entry_price: float
    province: str = ""


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    session_id: str
    kind: OrderKind
    side: Side
    stake: float
    price: float
    status: OrderStatus
    created_at: int
    province: str = ""


@dataclass(frozen=True, slots=True)
class Trade:
    id: str
    session_id: str
    bar_index: int
    side: Side
    stake: float
    price: float
    pnl: float
    created_at: int
    province: str = ""


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    season_id: str
    user_id: str
    group_id: str | None
    return_pct: float
    equity: float
    max_drawdown: float
    trades: int
    updated_at: int
