from __future__ import annotations

from nonebot_plugin_weather_fighter.render.charts import (
    DOWN_COLOR,
    FONT_PATH,
    MA20_COLOR,
    REGION_COLOR,
    UP_COLOR,
    build_html,
    build_option,
    moving_average,
    sample_bars,
    sample_header,
    sample_regional_line,
)


def test_sample_bars_deterministic_and_valid() -> None:
    first = sample_bars(96, seed=7)
    second = sample_bars(96, seed=7)
    assert first == second
    assert len(first) == 96
    for bar in first:
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.volume > 0


def test_moving_average() -> None:
    assert moving_average([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_option_is_chart_only() -> None:
    bars = sample_bars(96)
    option = build_option(bars)
    assert option["animation"] is False
    assert option["tooltip"] == {"show": False}
    assert "title" not in option
    assert "legend" not in option
    assert "dataZoom" not in option
    assert len(option["series"]) == 4
    candle = option["series"][0]
    assert candle["type"] == "candlestick"
    assert len(candle["data"]) == 96
    assert candle["itemStyle"]["borderColor"] == UP_COLOR
    assert candle["itemStyle"]["color0"] == DOWN_COLOR
    assert option["series"][2]["lineStyle"]["color"] == MA20_COLOR
    assert option["series"][3]["type"] == "bar"


def test_region_and_current_price_lines() -> None:
    bars = sample_bars(96)
    option = build_option(
        bars, regional_line=sample_regional_line(bars), current_price=bars[-1].close
    )
    assert len(option["series"]) == 5
    region_series = next(item for item in option["series"] if item["name"] == "区域")
    assert region_series["lineStyle"]["color"] == REGION_COLOR
    assert len(region_series["data"]) == 96
    marks = option["series"][0]["markLine"]["data"]
    assert any(mark["yAxis"] == bars[-1].close for mark in marks)


def test_build_html_has_local_echarts() -> None:
    bars = sample_bars(8)
    html = build_html(bars, regional_line=sample_regional_line(bars))
    assert "echarts.init" in html
    assert 'id="main-chart"' in html
    assert "<title>WXI</title>" in html
    assert len(html) > 500_000


def test_build_html_embeds_local_font() -> None:
    html = build_html(sample_bars(8))
    assert FONT_PATH.is_file()
    assert FONT_PATH.read_bytes()[:4] == b"wOF2"
    assert 'id="wf-font"' in html
    assert "new FontFace('WF Mono'" in html
    assert "document.fonts.add(face)" in html


def test_build_html_renders_quote_head() -> None:
    bars = sample_bars(96)
    html = build_html(
        bars,
        regional_line=sample_regional_line(bars),
        header=sample_header(bars),
    )
    assert 'class="quote-head"' in html
    assert "河北 · 石家庄" in html
    assert "今开" in html
    assert "舒适度" in html
    assert "交易中" in html
