from __future__ import annotations

import inspect
from typing import Any

from ..models import Bar
from .charts import build_html


async def _render_png(html: str, *, width: int, height: int, scale: float) -> bytes:
    """跨版本渲染：0.7+ 用 render_html，0.6 及更早用 html_to_pic。"""
    import nonebot_plugin_htmlrender as htmlrender

    render_html = getattr(htmlrender, "render_html", None)
    if render_html is not None:
        image = await render_html(
            html,
            width=width,
            height=height,
            device_pixel_ratio=scale,
            image_format="png",
        )
        return bytes(getattr(image, "data", image))

    html_to_pic = getattr(htmlrender, "html_to_pic", None)
    if html_to_pic is not None:
        options: dict[str, Any] = {}
        if "full_page" in inspect.signature(html_to_pic).parameters:
            options["full_page"] = False
        return await html_to_pic(
            html,
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
            type="png",
            **options,
        )

    raise RuntimeError("nonebot-plugin-htmlrender 版本不支持：缺少 render_html 与 html_to_pic")


async def render_main_chart(
    bars: list[Bar],
    *,
    regional_line: list[float | None] | None = None,
    ma5: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    current_price: float | None = None,
    header: dict[str, Any] | None = None,
    forecast: list[Bar] | None = None,
    width: int = 1200,
    height: int = 860,
    scale: float = 2.0,
) -> bytes:
    return await _render_png(
        build_html(
            bars,
            regional_line=regional_line,
            ma5=ma5,
            ma20=ma20,
            current_price=current_price,
            header=header,
            forecast=forecast,
        ),
        width=width,
        height=height,
        scale=scale,
    )
