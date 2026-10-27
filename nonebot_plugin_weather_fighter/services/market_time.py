from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
BAR_MINUTES = 15

# A 股交易时段：09:30-11:30、13:00-15:00；11:30-13:00 午休。
TRADING_WINDOWS: tuple[tuple[int, int], ...] = (
    (9 * 60 + 30, 11 * 60 + 30),
    (13 * 60, 15 * 60),
)
BARS_PER_DAY = sum((end - start) // BAR_MINUTES for start, end in TRADING_WINDOWS)

_OPEN_MINUTE = TRADING_WINDOWS[0][0]
_LUNCH_START = TRADING_WINDOWS[0][1]
_LUNCH_END = TRADING_WINDOWS[1][0]
_CLOSE_MINUTE = TRADING_WINDOWS[1][1]


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5


def is_trading_moment(value: datetime) -> bool:
    if not is_trading_day(value.date()):
        return False
    minutes = value.hour * 60 + value.minute + value.second / 60
    return any(start <= minutes < end for start, end in TRADING_WINDOWS)


def _window_bounds(day: date, start: int, end: int) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, clock_time(start // 60, start % 60), TZ),
        datetime.combine(day, clock_time(end // 60, end % 60), TZ),
    )


def trading_mask(times: tuple[str, ...]) -> list[int]:
    """返回只落在交易时段内的下标；自动跳过午休、夜间和周末。"""
    out: list[int] = []
    for index, value in enumerate(times):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            continue
        if not is_trading_day(moment.date()):
            continue
        minutes = moment.hour * 60 + moment.minute
        if any(start <= minutes < end for start, end in TRADING_WINDOWS):
            out.append(index)
    return out


def trading_minutes_between(start_epoch: float, end_epoch: float) -> float:
    """两个真实时间点之间真正处在交易时段内的分钟数。"""
    if end_epoch <= start_epoch:
        return 0.0
    start = datetime.fromtimestamp(start_epoch, TZ)
    end = datetime.fromtimestamp(end_epoch, TZ)
    total = 0.0
    day = start.date()
    while day <= end.date():
        if is_trading_day(day):
            for window_start, window_end in TRADING_WINDOWS:
                opened, closed = _window_bounds(day, window_start, window_end)
                overlap_start = max(start, opened)
                overlap_end = min(end, closed)
                if overlap_end > overlap_start:
                    total += (overlap_end - overlap_start).total_seconds() / 60
        day += timedelta(days=1)
    return total


def add_trading_minutes(start_epoch: float, minutes: float) -> float:
    """从一个真实时间点向后推移 N 个交易分钟，午休/夜间/周末不计时。"""
    if minutes <= 0:
        return start_epoch
    current = datetime.fromtimestamp(start_epoch, TZ)
    remaining = float(minutes)
    day = current.date()
    for _ in range(3660):
        if is_trading_day(day):
            for window_start, window_end in TRADING_WINDOWS:
                opened, closed = _window_bounds(day, window_start, window_end)
                cursor = max(current, opened)
                if cursor >= closed:
                    continue
                available = (closed - cursor).total_seconds() / 60
                if remaining <= available:
                    return (cursor + timedelta(minutes=remaining)).timestamp()
                remaining -= available
                current = closed
        day += timedelta(days=1)
    return current.timestamp()


def trading_bar_of_day(epoch: float | None = None) -> int:
    """当前真实时间落在交易日的第几根 15 分钟 bar（0..15）。"""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, TZ)
    if not is_trading_day(moment.date()):
        return BARS_PER_DAY - 1
    minutes = moment.hour * 60 + moment.minute
    morning_start, morning_end = TRADING_WINDOWS[0]
    afternoon_start, afternoon_end = TRADING_WINDOWS[1]
    morning_bars = (morning_end - morning_start) // BAR_MINUTES
    if minutes < morning_start:
        return 0
    if minutes < morning_end:
        return min(morning_bars - 1, (minutes - morning_start) // BAR_MINUTES)
    if minutes < afternoon_start:
        return morning_bars - 1
    if minutes < afternoon_end:
        return morning_bars + (minutes - afternoon_start) // BAR_MINUTES
    return BARS_PER_DAY - 1


def market_status(epoch: float | None = None) -> tuple[str, bool]:
    """返回（给玩家看的状态文案，当前是否在交易时段）。"""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, TZ)
    if is_trading_moment(moment):
        return "交易中", True
    if not is_trading_day(moment.date()):
        return "周末休市 · 下个交易日 09:30", False
    minutes = moment.hour * 60 + moment.minute
    if _LUNCH_START <= minutes < _LUNCH_END:
        return "午休 · 13:00 开盘", False
    if minutes < _OPEN_MINUTE:
        return "未开盘 · 09:30 开盘", False
    if minutes >= _CLOSE_MINUTE:
        return "已收盘 · 下个交易日 09:30", False
    return "休市 · 下个交易日 09:30", False
