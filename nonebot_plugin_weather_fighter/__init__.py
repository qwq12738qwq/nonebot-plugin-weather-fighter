from __future__ import annotations

import os

from nonebot import get_driver, get_plugin_config, logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")

_driver = get_driver()


def _playwright_cache_dir() -> str:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured

    from nonebot_plugin_localstore import get_plugin_cache_dir

    return str(get_plugin_cache_dir() / "ms-playwright")


if getattr(_driver.config, "render", None) is None:
    render_config: dict[str, object] = {"provider": "playwright"}
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if executable:
        render_config["provider_config"] = {
            "executable_path": executable,
            "launch_args": os.environ.get(
                "PLAYWRIGHT_CHROMIUM_ARGS", "--no-sandbox --disable-crashpad"
            ),
        }
    else:
        render_config["provider_config"] = {"storage_path": _playwright_cache_dir()}
    _driver.config.render = render_config  # type: ignore[attr-defined]

require("nonebot_plugin_htmlrender")

from . import consts  # noqa: E402
from . import matcher as matcher  # noqa: E402
from .config import Config  # noqa: E402
from .game.engine import get_engine  # noqa: E402
from .services.http import close_http, configure_http  # noqa: E402
from .services.openmeteo import OpenMeteoConfig, configure_client  # noqa: E402
from .services.provinces import resolve_province  # noqa: E402
from .services.store import close_store, init_store  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="天气战士",
    description="老实看天气,很枯燥吧",
    usage=consts.HELP_TEXT,
    type="application",
    config=Config,
    homepage="https://github.com/qwq12738qwq/nonebot-plugin-weather-fighter",
    supported_adapters={"~onebot.v11"},
)


@_driver.on_startup
async def _startup() -> None:
    config = get_plugin_config(Config)
    configure_http(config.weather_fighter_retry_count)
    configure_client(
        OpenMeteoConfig(
            bucket_seconds=config.weather_fighter_bucket_bars,
            stale_seconds=config.weather_fighter_stale_seconds,
            timeout=config.weather_fighter_http_timeout,
        )
    )
    await init_store()
    province = resolve_province(config.weather_fighter_default_province)
    if province is None:
        logger.warning("默认省份 {} 无效，跳过启动开盘", config.weather_fighter_default_province)
        return
    try:
        await get_engine().open_board(province)
    except Exception as exc:  # noqa: BLE001 - 启动开盘失败时等首次命令重试
        logger.opt(exception=exc).warning("启动开盘失败，首次命令时会重试")


@_driver.on_shutdown
async def _shutdown() -> None:
    await close_store()
    await close_http()
