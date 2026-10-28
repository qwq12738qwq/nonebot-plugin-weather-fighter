from __future__ import annotations

import base64
import json
import math
import random
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from ..models import Bar

_RENDER_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _RENDER_DIR / "templates"
_STATIC_DIR = _RENDER_DIR / "static"

FONT_FAMILY = "'WF Mono', 'DejaVu Sans Mono', 'Liberation Mono', monospace"
FONT_PATH = _STATIC_DIR / "fonts" / "weather-fighter.woff2"

UP_COLOR = "#ff4d4f"
DOWN_COLOR = "#22c55e"
MA5_COLOR = "#e6b450"
MA20_COLOR = "#4fa3ff"
REGION_COLOR = "#c792ea"
BASELINE_COLOR = "#8a6d1f"
CURRENT_COLOR = "rgba(255,255,255,.28)"


def moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index >= window - 1:
            out[index] = total / window
    return out


def sample_bars(count: int = 96, *, seed: int = 7) -> list[Bar]:
    """确定性的演示行情（气温 K 线）。"""
    rng = random.Random(seed)
    start = datetime(2026, 10, 20, 0, 0)
    price = 15.0
    bars: list[Bar] = []
    for index in range(count):
        wave = 3.0 * math.sin(index / 9.0) + 2.0 * math.sin(index / 23.0)
        move = wave * 0.05 + rng.gauss(0.0, 0.18)
        if rng.random() < 0.06:
            move += rng.choice((-1.0, 1.0)) * rng.uniform(0.4, 0.9)
        open_price = price
        close_price = max(0.1, open_price + move)
        high = max(open_price, close_price) + abs(rng.gauss(0.0, 0.08))
        low = min(open_price, close_price) - abs(rng.gauss(0.0, 0.08))
        volume = max(1.0, 70.0 + rng.gauss(0.0, 18.0) + abs(close_price - open_price) * 120.0)
        bars.append(
            Bar(
                time=(start + timedelta(minutes=15 * index)).strftime("%m-%d %H:%M"),
                open=round(open_price, 2),
                high=round(high, 2),
                low=round(max(0.1, low), 2),
                close=round(close_price, 2),
                volume=round(volume, 1),
            )
        )
        price = close_price
    return bars


def sample_regional_line(bars: list[Bar]) -> list[float]:
    return [
        bar.close + 0.2 * math.sin(index / 8.0) + 0.1 * math.sin(index / 21.0)
        for index, bar in enumerate(bars)
    ]


def build_option(
    bars: list[Bar],
    *,
    regional_line: list[float | None] | None = None,
    ma5: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    current_price: float | None = None,
    forecast: list[Bar] | None = None,
) -> dict[str, Any]:
    forecast = forecast or []
    times = [bar.time for bar in bars] + [bar.time for bar in forecast]
    candles: list[Any] = [[bar.open, bar.close, bar.low, bar.high] for bar in bars]
    forecast_candles = [[bar.open, bar.close, bar.low, bar.high] for bar in forecast]
    for bar, candle in zip(forecast, forecast_candles, strict=True):
        color = UP_COLOR if bar.close >= bar.open else DOWN_COLOR
        candles.append(
            {
                "value": candle,
                "itemStyle": {
                    "color": "rgba(0,0,0,0)",
                    "color0": "rgba(0,0,0,0)",
                    "borderColor": color,
                    "borderColor0": color,
                    "borderType": "dashed",
                    "borderWidth": 1.2,
                },
            }
        )
    closes = [bar.close for bar in bars]
    ma5 = list(ma5) if ma5 is not None else moving_average(closes, 5)
    ma20 = list(ma20) if ma20 is not None else moving_average(closes, 20)
    ma5 = ma5 + [None] * len(forecast)
    ma20 = ma20 + [None] * len(forecast)
    volumes = [
        {
            "value": bar.volume,
            "itemStyle": {"color": UP_COLOR if bar.close >= bar.open else DOWN_COLOR},
        }
        for bar in bars
    ]
    if regional_line is not None:
        regional_line = list(regional_line) + [None] * len(forecast)
    label_interval = max(0, len(times) // 8)

    axis_label = {
        "color": "#8b949e",
        "fontFamily": FONT_FAMILY,
        "fontSize": 11,
    }
    split_line = {"show": True, "lineStyle": {"color": "#1b2230", "type": "dashed"}}
    axis_line = {"show": True, "lineStyle": {"color": "#2a3140"}}
    marks: list[dict[str, Any]] = []
    if forecast and bars:
        marks.append(
            {
                "xAxis": bars[-1].time,
                "lineStyle": {"color": "rgba(255,255,255,.35)", "type": "dashed", "width": 1},
            }
        )
    if current_price is not None:
        marks.append(
            {
                "yAxis": current_price,
                "lineStyle": {"color": CURRENT_COLOR, "type": "solid", "width": 1},
            }
        )

    series: list[dict[str, Any]] = [
        {
            "name": "WXI",
            "type": "candlestick",
            "data": candles,
            "barWidth": "62%",
            "itemStyle": {
                "color": UP_COLOR,
                "borderColor": UP_COLOR,
                "borderWidth": 1,
                "color0": DOWN_COLOR,
                "borderColor0": DOWN_COLOR,
            },
            "markLine": {
                "symbol": "none",
                "silent": True,
                "label": {"show": False},
                "data": marks,
            },
            "z": 3,
        },
    ]
    if forecast:
        series[0]["markArea"] = {
            "silent": True,
            "itemStyle": {"color": "rgba(255,255,255,.035)"},
            "label": {
                "show": True,
                "position": "insideTop",
                "color": "#8b949e",
                "fontSize": 10,
                "formatter": "预测",
            },
            "data": [
                [
                    {"xAxis": forecast[0].time},
                    {"xAxis": forecast[-1].time},
                ]
            ],
        }
    series.extend(
        [
            {
                "name": "MA5",
                "type": "line",
                "data": ma5,
                "symbol": "none",
                "smooth": False,
                "connectNulls": False,
                "lineStyle": {"color": MA5_COLOR, "width": 1.2},
                "z": 4,
            },
            {
                "name": "MA20",
                "type": "line",
                "data": ma20,
                "symbol": "none",
                "smooth": False,
                "connectNulls": False,
                "lineStyle": {"color": MA20_COLOR, "width": 1.2},
                "z": 4,
            },
        ]
    )
    if regional_line is not None:
        series.append(
            {
                "name": "区域",
                "type": "line",
                "data": regional_line,
                "symbol": "none",
                "smooth": True,
                "connectNulls": False,
                "lineStyle": {"color": REGION_COLOR, "width": 1.2, "type": "dashed"},
                "z": 2,
            }
        )
    series.append(
        {
            "name": "活跃度",
            "type": "bar",
            "xAxisIndex": 1,
            "yAxisIndex": 1,
            "data": volumes,
            "barWidth": "62%",
            "z": 2,
        }
    )

    return {
        "animation": False,
        "backgroundColor": "#0b0e14",
        "textStyle": {
            "fontFamily": FONT_FAMILY,
            "color": "#8b949e",
        },
        "tooltip": {"show": False},
        "axisPointer": {"show": False},
        "grid": [
            {"left": 68, "right": 28, "top": 18, "height": "66%"},
            {"left": 68, "right": 28, "top": "76%", "height": "14%"},
        ],
        "xAxis": [
            {
                "type": "category",
                "data": times,
                "boundaryGap": True,
                "axisLine": axis_line,
                "axisTick": {"show": False},
                "axisLabel": {"show": False},
                "splitLine": {"show": False},
            },
            {
                "type": "category",
                "gridIndex": 1,
                "data": times,
                "boundaryGap": True,
                "axisLine": axis_line,
                "axisTick": {"show": False},
                "name": "时间",
                "nameLocation": "middle",
                "nameGap": 22,
                "nameTextStyle": {**axis_label, "fontSize": 10},
                "axisLabel": {
                    **axis_label,
                    "fontSize": 10,
                    "interval": label_interval,
                    "hideOverlap": True,
                },
                "splitLine": {"show": False},
            },
        ],
        "yAxis": [
            {
                "scale": True,
                "splitNumber": 5,
                "name": "气温(℃)",
                "nameLocation": "end",
                "nameGap": 8,
                "nameTextStyle": {**axis_label, "fontSize": 10},
                "axisLine": {"show": False},
                "axisTick": {"show": False},
                "axisLabel": {**axis_label, "formatter": "{value}℃"},
                "splitLine": split_line,
            },
            {
                "gridIndex": 1,
                "splitNumber": 2,
                "axisLine": {"show": False},
                "axisTick": {"show": False},
                "axisLabel": {**axis_label, "fontSize": 9},
                "splitLine": {"show": False},
            },
        ],
        "series": series,
    }


@lru_cache(maxsize=1)
def _template() -> Template:
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=False,
        keep_trailing_newline=True,
    )
    return environment.get_template("main_chart.html")


@lru_cache(maxsize=1)
def _assets() -> tuple[str, str, str]:
    echarts_js = (_STATIC_DIR / "echarts.min.js").read_text(encoding="utf-8")
    theme_css = (_STATIC_DIR / "theme.css").read_text(encoding="utf-8")
    font_data = base64.b64encode(FONT_PATH.read_bytes()).decode("ascii")
    return echarts_js, theme_css, font_data


def build_html(
    bars: list[Bar],
    *,
    regional_line: list[float | None] | None = None,
    ma5: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    current_price: float | None = None,
    header: dict[str, Any] | None = None,
    forecast: list[Bar] | None = None,
) -> str:
    option_json = json.dumps(
        build_option(
            bars,
            regional_line=regional_line,
            ma5=ma5,
            ma20=ma20,
            current_price=current_price,
            forecast=forecast,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    echarts_js, theme_css, font_data = _assets()
    return _template().render(
        option_json=option_json,
        echarts_js=echarts_js,
        theme_css=theme_css,
        font_data=font_data,
        header=header,
    )


def sample_header(bars: list[Bar]) -> dict[str, Any]:
    """演示截图用的报价头；真实游戏由 GameEngine.quote_head() 生成。"""
    last = bars[-1]
    previous = bars[-2] if len(bars) > 1 else last
    change = last.close - previous.close
    change_pct = change / previous.close * 100 if previous.close else 0.0
    day = bars[-96:] if len(bars) >= 96 else bars
    high = max(item.high for item in day)
    low = min(item.low for item in day)
    ma5 = sum(item.close for item in bars[-5:]) / min(5, len(bars))
    ma20 = sum(item.close for item in bars[-20:]) / min(20, len(bars))
    return {
        "title": "河北 · 石家庄",
        "subtitle": "101090101 · 河北省",
        "tags": ("晴", "第 8 天 10:15", "交易中"),
        "price": f"{last.close:.1f}",
        "change": f"{change:+.1f}",
        "change_pct": f"{change_pct:+.2f}%",
        "up": change >= 0,
        "groups": (
            (
                ("今日最高", f"{high:.1f}"),
                ("今日最低", f"{low:.1f}"),
                ("体感", f"{last.close - 1.0:.1f}℃"),
                ("全天波动", f"{high - low:.1f}℃"),
            ),
            (
                ("阵风", "2.7 m/s 2级"),
                ("降水", "0.0 mm"),
                ("湿度", "52%"),
                ("露点", "8.2℃"),
            ),
            (
                ("舒适度", "82"),
                ("天气", "晴"),
                ("CAPE", "0"),
                ("活跃度", "1.2"),
            ),
            (
                ("MA5", f"{ma5:.1f}"),
                ("MA20", f"{ma20:.1f}"),
                ("区域", f"{last.close + 0.1:.1f}"),
                ("区域涨跌", "+0.12%"),
            ),
            (
                ("今开", f"{day[0].open:.1f}"),
                ("昨收", f"{day[0].open - 0.4:.1f}"),
                ("昨高/低", f"{high + 1.2:.1f}/{low - 0.8:.1f}"),
                ("观测点", "8 个"),
            ),
        ),
    }
