from __future__ import annotations

from typing import Any

from ..models import Bar
from .charts import build_html


async def render_main_chart(
    bars: list[Bar],
    *,
    regional_line: list[float | None] | None = None,
    ma5: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    current_price: float | None = None,
    header: dict[str, Any] | None = None,
    width: int = 1200,
    height: int = 860,
    scale: float = 2.0,
) -> bytes:
    from nonebot_plugin_htmlrender import render_html

    image = await render_html(
        build_html(
            bars,
            regional_line=regional_line,
            ma5=ma5,
            ma20=ma20,
            current_price=current_price,
            header=header,
        ),
        width=width,
        height=height,
        device_pixel_ratio=scale,
        image_format="png",
    )
    return image.data
