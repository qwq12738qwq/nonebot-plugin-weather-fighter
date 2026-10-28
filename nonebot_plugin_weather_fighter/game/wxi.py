from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from ..services.market_time import BARS_PER_DAY, TZ
from ..services.openmeteo import ForecastDeviation, MinutelySeries, RegionSeries
from ..services.regions import city_amp, dish_scale

BASE = 1000.0
WARM_DAYS = 7
TRADE_DAYS = 8

# 原版 game.js 的可标定系数（WXI-lite：去掉 PM2.5/地震/台风/天文）
P = {
    "EMA_A": 0.05,
    "TREND_K": 8.0,
    "NOISE_K": 13.308,
    "NOISE_A": 0.45,
    "NOISE_BOOST": 1.42,
    "MICRO_K": 1.247,
    "MICRO_DECAY": 0.68,
    "JUMP_AT": 1.8,
    "JUMP_K": 28.279,
    "JUMP_DECAY": 0.78,
    "REG_K": 4.99,
    "DEW_K": 4.159,
    "FCST_K": 1.663,
    "DISH_K": 2.495,
    "DISH_DECAY": 0.96,
    "COMFORT_K": 8.0,
    "NOISE_AMP": 0.35,
    "INERTIA": 0.92,
    "FLOW_RATE": 1.2,
    "EVENT_K": 0.0003,
    "FLOW_DECAY": 0.9,
    "SPIKE_P": 0.00012,
    "SPIKE_DEPTH": 0.045,
    "CHAOS_P": 0.0004,
    "CHAOS_K": 0.0035,
    "WICK_K": 0.30,
}


@dataclass(frozen=True, slots=True)
class WeatherArrays:
    time: tuple[str, ...]
    temp: tuple[float | None, ...]
    gust: tuple[float | None, ...]
    precip: tuple[float | None, ...]
    wcode: tuple[float | None, ...]
    cape: tuple[float | None, ...]
    dew: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class WxiBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    activity: float
    cape: int
    gust: float
    precip: float
    wcode: int
    temp: float
    dew: float | None
    comfort: float
    regional: float | None
    forecast_dev: float | None = None


@dataclass(frozen=True, slots=True)
class WxiSeries:
    province_name: str
    province_short: str
    city_name: str
    city_id: str
    seed: int
    bars: tuple[WxiBar, ...]
    regional_line: tuple[float | None, ...]
    region_cities: tuple[str, ...]
    region_scope: str
    warm_bars: int
    trade_bars: int
    window_start: int

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def total_bars(self) -> int:
        return self.warm_bars + self.trade_bars

    def bar(self, relative_index: int) -> WxiBar:
        return self.bars[relative_index]


def _ramp(x: float | None, points: list[tuple[float, float]]) -> float:
    if x is None or not math.isfinite(x):
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    for index in range(1, len(points)):
        x0, y0 = points[index - 1]
        x1, y1 = points[index]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _weather_score(code: int) -> float:
    if code == 0:
        return 100.0
    if code == 1:
        return 95.0
    if code == 2:
        return 85.0
    if code == 3:
        return 78.0
    if code in (45, 48):
        return 55.0
    if code in (51, 53, 55):
        return 65.0
    if code in (56, 57):
        return 45.0
    if code in (61, 63):
        return 35.0
    if code == 65:
        return 10.0
    if code in (66, 67):
        return 8.0
    if code in (71, 73, 75, 77):
        return 25.0
    if code in (80, 81):
        return 40.0
    if code == 82:
        return 12.0
    if code in (85, 86):
        return 20.0
    if code == 95:
        return 6.0
    if code in (96, 99):
        return 0.0
    return 60.0


COMFORT_WEIGHTS: dict[str, float] = {
    "temp": 0.34,
    "dew": 0.15,
    "precip": 0.21,
    "code": 0.20,
    "gust": 0.05,
    "air": 0.05,
}


def bar_scores(
    temp: float | None,
    dew: float | None,
    precip: float | None,
    code: float | None,
    gust: float | None,
) -> dict[str, float]:
    temp_value = float(temp) if temp is not None else 24.0
    dew_value = float(dew) if dew is not None else temp_value - 8.0
    precip_value = float(precip) if precip is not None else 0.0
    gust_value = float(gust) if gust is not None else 0.0
    code_value = int(code) if code is not None else 0
    return {
        "temp": _ramp(
            temp_value,
            [
                (-5, 0),
                (0, 10),
                (8, 40),
                (16, 80),
                (22, 100),
                (27, 100),
                (31, 70),
                (35, 35),
                (40, 0),
                (45, 0),
            ],
        ),
        "dew": _ramp(
            dew_value,
            [(0, 100), (10, 100), (16, 95), (20, 75), (24, 40), (27, 15), (30, 0)],
        ),
        "precip": _ramp(
            precip_value,
            [(0, 100), (0.05, 90), (0.3, 65), (0.8, 40), (2, 15), (5, 0), (20, 0)],
        ),
        "code": _weather_score(code_value),
        "gust": _ramp(
            gust_value,
            [(0, 100), (15, 98), (25, 88), (40, 62), (60, 28), (80, 10), (110, 0)],
        ),
        # 原版没有 PM2.5 时空气项按 85 分计入；保留这一项让 comfort 标尺一致。
        "air": 85.0,
    }


def comfort_from_scores(scores: dict[str, float]) -> float:
    return sum(COMFORT_WEIGHTS[key] * value for key, value in scores.items())


def comfort_values(arrays: WeatherArrays) -> list[float]:
    return [
        comfort_from_scores(
            bar_scores(
                arrays.temp[index],
                arrays.dew[index],
                arrays.precip[index],
                arrays.wcode[index],
                arrays.gust[index],
            )
        )
        for index in range(len(arrays.time))
    ]


def severity(comfort: list[float]) -> list[float]:
    n = len(comfort)
    out = [0.0] * n
    window = BARS_PER_DAY
    for index in range(n):
        start = max(0, index - window)
        end = min(n, index + window + 1)
        med = median(comfort[start:end])
        out[index] = -(comfort[index] - med) / 10.0
    return out


def ema(values: list[float], alpha: float) -> list[float]:
    if not values:
        return []
    out = [0.0] * len(values)
    current = values[0]
    for index, value in enumerate(values):
        current = alpha * value + (1 - alpha) * current
        out[index] = current
    return out


def arrays_from_minutely(series: MinutelySeries) -> WeatherArrays:
    return WeatherArrays(
        time=series.time,
        temp=series.temp,
        gust=series.gust,
        precip=series.precip,
        wcode=series.wcode,
        cape=series.cape,
        dew=series.dew,
    )


def align_region(local_time: tuple[str, ...], region: RegionSeries | None) -> WeatherArrays | None:
    if region is None or not region.time:
        return None
    index = {value: position for position, value in enumerate(region.time)}
    temp: list[float | None] = []
    gust: list[float | None] = []
    precip: list[float | None] = []
    wcode: list[float | None] = []
    cape: list[float | None] = []
    dew: list[float | None] = []
    hits = 0
    for value in local_time:
        position = index.get(value)
        if position is None:
            temp.append(None)
            gust.append(None)
            precip.append(0.0)
            wcode.append(0.0)
            cape.append(None)
            dew.append(None)
            continue
        temp.append(region.temp[position])
        gust.append(region.gust[position])
        precip.append(region.precip[position] or 0.0)
        wcode.append(region.wcode[position] or 0.0)
        cape.append(region.cape[position])
        dew.append(region.dew[position])
        if region.gust[position] is not None:
            hits += 1
    if hits < len(local_time) * 0.8:
        return None
    return WeatherArrays(
        time=local_time,
        temp=tuple(temp),
        gust=tuple(gust),
        precip=tuple(precip),
        wcode=tuple(wcode),
        cape=tuple(cape),
        dew=tuple(dew),
    )


def pick_window_start(
    times: tuple[str, ...],
    seed: int,
    *,
    warm_days: int = WARM_DAYS,
    trade_days: int = TRADE_DAYS,
    now: datetime | None = None,
) -> int:
    need = (warm_days + trade_days) * BARS_PER_DAY
    current = (now or datetime.now(TZ)).strftime("%Y-%m-%dT%H:%M")
    available_end = len(times)
    for index, value in enumerate(times):
        if value >= current:
            available_end = index
            break
    if available_end < need + 1:
        raise ValueError(f"行情历史不足：需要 {need} 根，当前只有 {available_end} 根")
    rng = random.Random(seed)
    return rng.randint(0, available_end - need - 1)


def _city_weight(province_name: str) -> float:
    if province_name in {"北京市", "上海市", "天津市", "重庆市"}:
        return 3.0
    return 2.2


def forecast_series(
    local_time: tuple[str, ...], forecast: ForecastDeviation | None
) -> tuple[list[float | None], list[float] | None]:
    if forecast is None or not forecast.time:
        return [None] * len(local_time), None
    positions = {value: index for index, value in enumerate(forecast.time)}
    raw: list[float | None] = []
    values: list[float] = []
    for value in local_time:
        position = positions.get(value)
        if position is None:
            raw.append(None)
            continue
        actual = forecast.actual[position]
        issued = forecast.forecast[position]
        if actual is None or issued is None:
            raw.append(None)
            continue
        deviation = float(actual) - float(issued)
        raw.append(deviation)
        values.append(deviation)
    if len(values) <= len(local_time) * 0.5:
        return raw, None
    center = median(values)
    scale = 1.4826 * median([abs(value - center) for value in values]) or 1.0
    normalized = [0.0 if value is None else (value - center) / scale for value in raw]
    return raw, normalized


def build_wxi_series(
    local: MinutelySeries,
    region: RegionSeries | None,
    *,
    province_name: str,
    province_short: str,
    city_name: str,
    city_id: str,
    seed: int,
    window_start: int,
    warm_days: int = WARM_DAYS,
    trade_days: int = TRADE_DAYS,
    forecast: ForecastDeviation | None = None,
) -> WxiSeries:
    warm_bars = warm_days * BARS_PER_DAY
    trade_bars = trade_days * BARS_PER_DAY
    need = warm_bars + trade_bars
    if window_start < 0 or window_start + need > len(local.time):
        raise ValueError("WXI 窗口越界")

    arrays = arrays_from_minutely(local)
    comfort = comfort_values(arrays)
    sev = severity(comfort)
    trend = ema(sev, P["EMA_A"])
    comfort_slow = ema(comfort, 1 - math.pow(0.5, 1 / BARS_PER_DAY))
    window = comfort_slow[window_start : window_start + need]
    comfort_median = median(window)

    fcst_raw, fcst_norm = forecast_series(local.time, forecast)

    region_window = align_region(local.time[window_start : window_start + need], region)
    region_sev: list[float] | None = None
    region_trend: list[float] | None = None
    if region_window is not None:
        region_sev = severity(comfort_values(region_window))
        region_trend = ema(region_sev, P["EMA_A"])

    dew_values = [value for value in arrays.dew if value is not None]
    if dew_values:
        dew_median = median(dew_values)
        dew_scale = 1.4826 * median([abs(value - dew_median) for value in dew_values]) or 1.0
    else:
        dew_median = 0.0
        dew_scale = 1.0

    weight = _city_weight(province_name)
    amp = city_amp(weight)
    dscale = dish_scale(weight) * P["DISH_K"]
    rng = random.Random(seed ^ 0x5EED)
    carry = 0.0
    dish = 0.0
    noise_ema = 0.0
    micro = 0.0
    calm = 0.0
    impact = 0.0
    flow = 0.0
    heat_ema = 0.0

    for step in range(200, 0, -1):
        key = max(0, window_start - step)
        anchor = BASE + (comfort_slow[key] - comfort_median) * P["COMFORT_K"]
        shock = (rng.random() - 0.5) * 2 * BASE * 0.002 * amp * P["NOISE_AMP"]
        impact = impact * (1 - P["INERTIA"]) + shock

    closes: list[float] = []
    seeds: list[tuple[int, float, float, int, float, float | None, float, float | None]] = []
    region_line: list[float | None] = []
    for relative in range(need):
        key = window_start + relative
        delta_sev = sev[key] - sev[key - 1] if key > 0 else 0.0
        shock = 0.0
        if abs(delta_sev) > P["JUMP_AT"]:
            shock = (abs(delta_sev) - P["JUMP_AT"]) * P["JUMP_K"] * (1 if delta_sev > 0 else -1)
        carry = carry * P["JUMP_DECAY"] + shock

        region_fast = 0.0
        if region_sev is not None and region_trend is not None:
            region_fast = (region_sev[relative] - region_trend[relative]) * P["REG_K"]

        dew = arrays.dew[key]
        dew_norm = 0.0 if dew is None else (dew - dew_median) / dew_scale

        dish = dish * P["DISH_DECAY"] + (rng.random() - 0.5) * 2 * dscale
        micro = micro * P["MICRO_DECAY"] + (rng.random() - 0.5) * 2 * P["MICRO_K"]
        noise_ema = noise_ema * P["NOISE_A"] + (sev[key] - trend[key]) * (1 - P["NOISE_A"])
        noise_term = noise_ema * P["NOISE_K"] * P["NOISE_BOOST"]

        flow_ret = impact / BASE
        flow = flow * P["FLOW_DECAY"] + abs(flow_ret) * 60
        arrival = P["FLOW_RATE"] * (1 + min(6.0, flow)) * (1 + calm / 30)
        n_events = (
            int(arrival) + (1 if rng.random() < (arrival % 1) else 0)
            if arrival > 3
            else (1 if rng.random() < arrival else 0)
        )
        event_sum = 0.0
        for _ in range(n_events):
            fcst_term = fcst_norm[key] * P["FCST_K"] if fcst_norm is not None else 0.0
            bias = (
                (
                    noise_term
                    + carry
                    + region_fast
                    + dew_norm * P["DEW_K"]
                    + dish
                    + micro
                    + fcst_term
                )
                * amp
                * P["NOISE_AMP"]
            )
            magnitude = BASE * P["EVENT_K"] * (rng.random() ** 3 * 4.5 + 0.05) * amp
            event_sum += (1 if bias >= 0 else -1) * magnitude + bias * 0.35

        heat = 0.0 if len(closes) < 2 else abs(closes[-1] - closes[-2]) / BASE
        heat_ema = heat_ema * 0.94 + heat * 0.06
        calm = min(60.0, calm + 1) if heat < 0.0008 else 0.0
        if rng.random() < P["CHAOS_P"] * (1 + calm / 25) * (1 + heat_ema * 400):
            tail = rng.random() ** 3 * 5.5 + 0.08
            event_sum += (1 if rng.random() < 0.5 else -1) * BASE * P["CHAOS_K"] * tail * amp
            calm = 0.0

        impact = impact * (1 - P["INERTIA"]) + event_sum
        anchor = BASE + (comfort_slow[key] - comfort_median) * P["COMFORT_K"]
        price = anchor + impact
        closes.append(price)

        cape = int(arrays.cape[key] or 0)
        gust = float(arrays.gust[key] or 0.0)
        precip = float(arrays.precip[key] or 0.0)
        code = int(arrays.wcode[key] or 0)
        temp = float(arrays.temp[key] or 0.0)
        activity = max(
            0.4,
            1 + precip * 10 + max(0.0, gust - 6) * 2 + cape / 60 + abs(dish) * 0.5,
        )
        regional_value = BASE + region_fast * 3 + dish * 0.2 if region_sev is not None else None
        seeds.append((cape, gust, precip, code, temp, dew, activity, regional_value))
        region_line.append(regional_value)

    bars: list[WxiBar] = []
    for relative, close in enumerate(closes):
        open_price = closes[relative - 1] if relative > 0 else close
        cape, gust, precip, code, temp, dew, activity, regional_value = seeds[relative]
        body = abs(close - open_price)
        wick = body * P["WICK_K"]
        activation = min(3.0, activity / 4)
        if rng.random() < P["SPIKE_P"] * (0.5 + activation):
            tail = min(2.0, rng.random() ** 2.5 * 2.6 + 0.06)
            wick += close * P["SPIKE_DEPTH"] * tail / dish_scale(weight)
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        bars.append(
            WxiBar(
                time=local.time[window_start + relative],
                open=open_price,
                high=high,
                low=low,
                close=close,
                activity=activity,
                cape=cape,
                gust=gust,
                precip=precip,
                wcode=code,
                temp=temp,
                dew=dew,
                comfort=comfort[window_start + relative],
                regional=regional_value,
                forecast_dev=fcst_raw[window_start + relative],
            )
        )

    return WxiSeries(
        province_name=province_name,
        province_short=province_short,
        city_name=city_name,
        city_id=city_id,
        seed=seed,
        bars=tuple(bars),
        regional_line=tuple(region_line),
        region_cities=tuple(region.cities) if region is not None else (),
        region_scope=region.scope if region is not None else "",
        warm_bars=warm_bars,
        trade_bars=trade_bars,
        window_start=window_start,
    )
