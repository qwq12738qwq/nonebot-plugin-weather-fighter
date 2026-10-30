from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
BAR_MINUTES = 15
# 全时段 24×7：每天 96 根 15 分钟 bar，没有午休与休市日
BARS_PER_DAY = 24 * 60 // BAR_MINUTES
_BAR_SECONDS = BAR_MINUTES * 60


def bar_of_day(epoch: float | None = None) -> int:
    """当前时间落在当天的第几根 bar（0..95）。"""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, TZ)
    return min(BARS_PER_DAY - 1, (moment.hour * 60 + moment.minute) // BAR_MINUTES)


def elapsed_bars(start: float, end: float) -> int:
    """(start, end] 内开始过的 bar 数；全时段，直接按 15 分钟对齐。"""
    if end <= start:
        return 0
    return int(end // _BAR_SECONDS) - int(start // _BAR_SECONDS)


def market_status(epoch: float | None = None) -> tuple[str, bool]:
    """全时段连续交易，没有休市；保留 (文案, 是否开市) 结构给行情头使用。"""
    return "交易中", True
