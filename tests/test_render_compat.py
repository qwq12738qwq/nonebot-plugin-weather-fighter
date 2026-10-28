from __future__ import annotations

import sys
import types

import pytest

from nonebot_plugin_weather_fighter.render import renderer


def _install(monkeypatch: pytest.MonkeyPatch, **attrs: object) -> None:
    module = types.ModuleType("nonebot_plugin_htmlrender")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "nonebot_plugin_htmlrender", module)


class _Image:
    def __init__(self, data: bytes) -> None:
        self.data = data


@pytest.mark.asyncio
async def test_uses_render_html_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    async def render_html(html: str, **kwargs: object) -> _Image:
        calls["html"] = html
        calls.update(kwargs)
        return _Image(b"new")

    _install(monkeypatch, render_html=render_html)
    out = await renderer._render_png("<p>x</p>", width=1200, height=860, scale=2.0)
    assert out == b"new"
    assert calls["width"] == 1200
    assert calls["height"] == 860
    assert calls["device_pixel_ratio"] == 2.0
    assert calls["image_format"] == "png"


@pytest.mark.asyncio
async def test_falls_back_without_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    async def html_to_pic(
        html: str,
        wait: int = 0,
        type: str = "png",
        device_scale_factor: float = 2,
        **kwargs: object,
    ) -> bytes:
        calls["device_scale_factor"] = device_scale_factor
        calls["kwargs"] = kwargs
        return b"old"

    _install(monkeypatch, html_to_pic=html_to_pic)
    out = await renderer._render_png("<p>x</p>", width=800, height=600, scale=1.5)
    assert out == b"old"
    assert calls["device_scale_factor"] == 1.5
    assert calls["kwargs"]["viewport"] == {"width": 800, "height": 600}
    assert "full_page" not in calls["kwargs"]


@pytest.mark.asyncio
async def test_falls_back_with_full_page_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    async def html_to_pic(html: str, full_page: bool = True, **kwargs: object) -> bytes:
        calls["full_page"] = full_page
        calls["kwargs"] = kwargs
        return b"old2"

    _install(monkeypatch, html_to_pic=html_to_pic)
    out = await renderer._render_png("<p>x</p>", width=800, height=600, scale=1.0)
    assert out == b"old2"
    assert calls["full_page"] is False
