from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .. import consts
from .market_time import TZ
from .openmeteo import MinutelySeries
from .provinces import Province


@dataclass(frozen=True)
class WeatherReport:
    province: Province
    observed_at: str
    temp: float
    dew: float | None
    precip: float
    wcode: int
    gust: float
    cape: float
    high: float | None
    low: float | None
    next_bars: tuple[tuple[str, float, int], ...]
    source: str


def _current_index(series: MinutelySeries, moment: datetime) -> int:
    target = moment.strftime("%Y-%m-%dT%H:%M")
    index = 0
    for position, value in enumerate(series.time):
        if value <= target:
            index = position
        else:
            break
    return index


def build_weather_report(
    series: MinutelySeries,
    province: Province,
    *,
    now: datetime | None = None,
    future_bars: int = 4,
) -> WeatherReport:
    if not series.time:
        raise ValueError("天气序列为空")
    moment = now or datetime.now(TZ)
    index = _current_index(series, moment)
    observed = series.time[index]
    day = observed[:10]
    day_indices = [i for i, value in enumerate(series.time) if value.startswith(day)]
    temps: list[float] = []
    for position in day_indices:
        value = series.temp[position]
        if value is not None:
            temps.append(float(value))
    current_temp = series.temp[index]
    high = max(temps) if temps else None
    low = min(temps) if temps else None
    upcoming: list[tuple[str, float, int]] = []
    for position in range(index + 1, min(len(series.time), index + 1 + future_bars)):
        temp = series.temp[position]
        if temp is None:
            continue
        upcoming.append(
            (series.time[position][11:16], float(temp), int(series.wcode[position] or 0))
        )
    return WeatherReport(
        province=province,
        observed_at=f"{observed[5:10]} {observed[11:16]}",
        temp=float(current_temp) if current_temp is not None else 0.0,
        dew=series.dew[index],
        precip=float(series.precip[index] or 0.0),
        wcode=int(series.wcode[index] or 0),
        gust=float(series.gust[index] or 0.0),
        cape=float(series.cape[index] or 0.0),
        high=high,
        low=low,
        next_bars=tuple(upcoming),
        source=series.source,
    )


def _source_label(source: str) -> str:
    if "open-meteo" in source:
        return "Open-Meteo"
    return source or "Open-Meteo"


def format_weather_report(report: WeatherReport) -> str:
    dew = "—" if report.dew is None else f"{report.dew:.1f}℃"
    high_low = (
        "—"
        if report.high is None or report.low is None
        else f"{report.low:.1f} ~ {report.high:.1f}℃"
    )
    lines = [
        f"{report.province.name} · {report.province.capital} 天气　{report.observed_at}",
        f"现在 {report.temp:.1f}℃ · {consts.weather_icon(report.wcode)} "
        f"{consts.weather_name(report.wcode)} · 露点 {dew} · "
        f"阵风 {report.gust:.1f}m/s（{consts.wind_level(report.gust)}级）",
        f"今日 {high_low} · 降水 {report.precip:.1f}mm · CAPE {report.cape:.0f}",
    ]
    if report.next_bars:
        upcoming = " · ".join(
            f"{time} {temp:.1f}℃ {consts.weather_name(code)}"
            for time, temp, code in report.next_bars
        )
        lines.append(f"未来 1 小时：{upcoming}")
    lines.append(f"数据：{_source_label(report.source)} 15 分钟实况/预报")
    return "\n".join(lines)
