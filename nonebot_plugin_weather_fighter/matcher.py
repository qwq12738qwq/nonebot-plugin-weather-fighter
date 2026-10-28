from __future__ import annotations

import re

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from . import consts
from .game.amount import AmountError, parse_amount
from .game.engine import GameEngine, GameError, GameState, QuoteStats, get_engine
from .models import Order, OrderKind, OrderStatus, Side
from .services.openmeteo import get_client
from .services.provinces import Province, province_names, resolve_province
from .services.weather import build_weather_report, format_weather_report

help_cmd = on_command(consts.CMD_HELP, aliases={"help", "?"}, priority=10, block=True)
weather_cmd = on_command(
    consts.CMD_WEATHER,
    aliases={"行情", "大盘", "K线", "k线", "查天气", "气温", "天气查询"},
    priority=10,
    block=True,
)
long_cmd = on_command(consts.CMD_LONG, priority=10, block=True)
short_cmd = on_command(consts.CMD_SHORT, priority=10, block=True)
close_cmd = on_command(consts.CMD_CLOSE, priority=10, block=True)
order_cmd = on_command(consts.CMD_ORDER, aliases={"挂单", "挂"}, priority=10, block=True)
cancel_cmd = on_command(consts.CMD_CANCEL, aliases={"取消挂单"}, priority=10, block=True)
leverage_cmd = on_command(consts.CMD_LEVERAGE, aliases={"杠杆", "倍率"}, priority=10, block=True)

LONG_WORDS = {"买入"}
SHORT_WORDS = {"卖出"}
CLOSE_WORDS = {"平", "平仓"}
LEVERAGE_WORDS = {"倍数", "杠杆", "倍率"}
LEVERAGE_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)(?:倍|[xX])")


def _province_list() -> str:
    return "、".join(province_names())


def _split_leverage(rest: list[str]) -> tuple[float | None, list[str]]:
    leverage: float | None = None
    remaining: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in LEVERAGE_WORDS:
            if index + 1 >= len(rest):
                raise AmountError("倍数写法：倍数 5")
            try:
                leverage = float(rest[index + 1].removesuffix("倍"))
            except ValueError as exc:
                raise AmountError("倍数格式不对捏，例如：倍数 5") from exc
            index += 2
            continue
        match = LEVERAGE_PATTERN.fullmatch(token)
        if match:
            leverage = float(match.group(1))
            index += 1
            continue
        remaining.append(token)
        index += 1
    return leverage, remaining


def _split_province(text: str) -> tuple[Province | None, list[str]]:
    province: Province | None = None
    rest: list[str] = []
    for part in text.split():
        found = resolve_province(part)
        if found is not None and province is None:
            province = found
        else:
            rest.append(part)
    return province, rest


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _signed(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _side_label(side: Side) -> str:
    return "买入" if side is Side.LONG else "卖出"


def _province_short(name: str) -> str:
    province = resolve_province(name)
    return province.short if province is not None else name


def _order_label(order: Order) -> str:
    short = _province_short(order.province)
    if order.kind is OrderKind.BET:
        return f"{short} 到价 {order.price:.1f} {_side_label(order.side)} {_money(order.stake)}"
    return f"{short} 到价 {order.price:.1f} 平仓"


def _weather_line(state: GameState) -> str:
    bar = state.current
    dew = "—" if bar.dew is None else f"{bar.dew:.1f}℃"
    return (
        f"{consts.weather_icon(bar.wcode)} {consts.weather_name(bar.wcode)} · "
        f"露点 {dew} · 阵风 {bar.gust:.1f}m/s（{consts.wind_level(bar.gust)}级） · "
        f"降水 {bar.precip:.1f}mm · 舒适度 {bar.comfort:.0f}"
    )


def _quote_text(state: GameState, stats: QuoteStats) -> str:
    lines = [
        f"{state.wxi.province_short} · {state.wxi.city_name} 气温　"
        f"{stats.price:.1f}℃　{_signed(stats.change)}（{_pct(stats.change_pct)}）",
        f"{stats.time_label} · {stats.session_label} · {stats.market_status} · 全局盘",
        _weather_line(state),
    ]
    if state.wxi.region_cities:
        cities = "、".join(state.wxi.region_cities[:8])
        lines.append(f"区域 {_fmt(stats.regional)}（{_pct(stats.regional_change_pct)}）· {cities}")
    if state.positions:
        parts = []
        for position in state.positions:
            short = _province_short(position.province)
            parts.append(
                f"{short} {_side_label(position.side)} {_money(position.stake)}"
                f" @ {position.entry_price:.1f}℃（{state.pnl_for(position):+,.0f}）"
            )
        lines.append("持仓：" + "；".join(parts))
        lines.append(
            f"余额 {_money(state.account.cash)}　总投入 {_money(state.total_stake)}"
            f"　总浮盈 {state.position_pnl:+,.0f}　总资产 {_money(state.account.equity)}"
        )
    else:
        lines.append(
            f"余额 {_money(state.account.cash)}　总资产 {_money(state.account.equity)}　空仓"
        )
    if state.safety_grant > 0:
        lines.append(f"今日保底 +{_money(state.safety_grant)}（余额不足，自动补充）")
    elif state.session.safety_active:
        lines.append("保底生效中：每个真实自然日自动补充，达到目标后停止")
    open_orders = [order for order in state.orders if order.status is OrderStatus.OPEN]
    if open_orders:
        lines.append("到价单：" + "；".join(_order_label(order) for order in open_orders))
    if state.ended and state.session.return_pct is not None:
        lines.append(f"你的这一盘已结束：收益 {_pct(state.session.return_pct * 100)}")
    return "\n".join(lines)


def _event_ids(event: MessageEvent) -> tuple[str, str | None]:
    group_id = getattr(event, "group_id", None)
    return str(event.get_user_id()), str(group_id) if group_id is not None else None


@help_cmd.handle()
async def _(args: Message = CommandArg()) -> None:
    text = str(args).strip()
    if text:
        province = resolve_province(text)
        if province is not None:
            await help_cmd.finish(
                f"{province.name}（{province.short}）\n"
                f"代表城市：{province.capital}　city_id：{province.city_id}\n"
                f"国家站：{province.cma_station or '—'}　"
                f"坐标：{province.coords[0]:.4f}, {province.coords[1]:.4f}"
            )
    await help_cmd.finish(consts.HELP_TEXT + f"\n可用省份：{_province_list()}")


@weather_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    user_id, group_id = _event_ids(event)
    province, _ = _split_province(str(args).strip())
    engine = get_engine()
    if province is None:
        province = await engine.last_province(user_id, group_id)
    if province is None:
        province = await engine.default_province()
    if province is None:
        await weather_cmd.finish(
            f"用法：{consts.CMD_WEATHER} 河北（也可以发城市名，如 石家庄）\n"
            f"可用省份：{_province_list()}"
        )

    weather_text = ""
    try:
        series = (await get_client().fetch_minutely_15(province)).value
        weather_text = format_weather_report(build_weather_report(series, province))
    except Exception as exc:  # noqa: BLE001 - 天气取数失败时仍尝试出图
        logger.opt(exception=exc).warning("天气取数失败，仅发送主图")

    quote_text = ""
    png: bytes | None = None
    try:
        state = await engine.ensure_game(user_id, group_id, province)
        quote_text = _quote_text(state, engine.quote_stats(state))
        png = await _render_chart(engine, state)
    except GameError as exc:
        await weather_cmd.finish(str(exc))
    except Exception as exc:  # noqa: BLE001 - 渲染失败时退回纯文字
        logger.opt(exception=exc).warning("主图渲染失败，改为纯文字")

    text = "\n\n".join(part for part in (quote_text, weather_text) if part)
    if png is None:
        if not text:
            await weather_cmd.finish("天气取数失败，请稍后再试")
        await weather_cmd.finish(text)
    content: Message = MessageSegment.image(png) + Message("")
    if text:
        content += Message("\n" + text)
    await weather_cmd.finish(content)


async def _render_chart(engine: GameEngine, state: GameState) -> bytes:
    forecast = await engine.chart_forecast(state)
    chart = engine.chart_data(state, forecast)
    stats = engine.quote_stats(state)
    from .render.renderer import render_main_chart

    return await render_main_chart(
        list(chart.bars),
        regional_line=list(chart.regional),
        ma5=list(chart.ma5),
        ma20=list(chart.ma20),
        current_price=chart.current_price,
        header=engine.quote_head(state, stats),
        forecast=list(chart.forecast),
    )


@leverage_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    user_id, group_id = _event_ids(event)
    engine = get_engine()
    raw = str(args).strip().lower().removesuffix("倍").removesuffix("x").strip()
    if not raw:
        try:
            state = await engine.ensure_game(user_id, group_id, None)
        except GameError as exc:
            await leverage_cmd.finish(str(exc))
        await leverage_cmd.finish(
            f"当前 {state.session.leverage:g} 倍，可选 1~{engine.max_leverage:g} 倍。\n"
            f"用法：{consts.CMD_LEVERAGE} 10"
        )
    try:
        value = float(raw)
    except ValueError:
        await leverage_cmd.finish(f"用法：{consts.CMD_LEVERAGE} 10（1~{engine.max_leverage:g} 倍）")
    try:
        state = await engine.set_leverage(user_id, group_id, value)
    except GameError as exc:
        await leverage_cmd.finish(str(exc))
    await leverage_cmd.finish(
        f"已设置 {state.session.leverage:g} 倍（上限 {engine.max_leverage:g} 倍）"
    )


async def _close_and_reply(
    engine: GameEngine,
    user_id: str,
    group_id: str | None,
    province: Province | None,
    matcher: type[Matcher],
) -> None:
    try:
        outcome = await engine.close_position(user_id, group_id, province)
    except GameError as exc:
        await matcher.finish(str(exc))
    if not outcome.trades or outcome.state is None:
        await matcher.finish("当前没有持仓")
    parts = []
    for trade in outcome.trades:
        short = _province_short(trade.province)
        word = "赚" if trade.pnl >= 0 else "亏"
        parts.append(f"{short} {word} {trade.pnl:+,.0f}")
    await matcher.finish(
        f"已平仓：{'；'.join(parts)} · 合计 {outcome.total_pnl:+,.0f} · "
        f"余额 {_money(outcome.state.account.cash)}"
    )


async def _trade(event: MessageEvent, side: Side, matcher: type[Matcher], args: Message) -> None:
    user_id, group_id = _event_ids(event)
    province, rest = _split_province(str(args).strip())
    try:
        leverage, rest = _split_leverage(rest)
    except AmountError as exc:
        await matcher.finish(str(exc))
    engine = get_engine()
    amount_text = "".join(rest)
    if side is Side.SHORT and not amount_text:
        if leverage is not None:
            await matcher.finish("卖出不写金额是平仓，不能带倍数；做空请写金额。")
        await _close_and_reply(engine, user_id, group_id, province, matcher)
        return
    try:
        state = await engine.ensure_game(user_id, group_id, province)
    except GameError as exc:
        await matcher.finish(f"{exc}\n可用省份：{_province_list()}")
    if leverage is not None:
        try:
            state = await engine.set_leverage(user_id, group_id, leverage)
        except GameError as exc:
            await matcher.finish(str(exc))
    try:
        amount = parse_amount(amount_text, state.account.cash)
    except AmountError as exc:
        await matcher.finish(str(exc))
    try:
        outcome = await engine.open_position(user_id, group_id, province, side, stake=amount)
    except GameError as exc:
        await matcher.finish(str(exc))
    market = outcome.state.wxi
    await matcher.finish(
        f"已{_side_label(side)} {_money(outcome.trade.stake)} · "
        f"{market.province_short}/{market.city_name} · "
        f"{outcome.state.session.leverage:g} 倍 · 成交 {outcome.trade.price:.1f}℃"
    )


@long_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    await _trade(event, Side.LONG, long_cmd, args)


@short_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    await _trade(event, Side.SHORT, short_cmd, args)


@close_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    user_id, group_id = _event_ids(event)
    province, _ = _split_province(str(args).strip())
    await _close_and_reply(get_engine(), user_id, group_id, province, close_cmd)


@order_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()) -> None:
    user_id, group_id = _event_ids(event)
    province, rest = _split_province(str(args).strip())
    usage = (
        f"用法：{consts.CMD_ORDER} 河北 16.5 买入 1000 / "
        f"{consts.CMD_ORDER} 河北 16.5 卖出 1000 / {consts.CMD_ORDER} 河北 18.0 平"
    )
    if len(rest) < 2:
        await order_cmd.finish(usage)
    try:
        price = float(rest[0])
    except ValueError:
        await order_cmd.finish(usage)
    action = rest[1]
    amount_text = "".join(rest[2:])
    engine = get_engine()

    if action in CLOSE_WORDS:
        try:
            await engine.place_order(user_id, group_id, province, OrderKind.CLOSE, None, price)
        except GameError as exc:
            await order_cmd.finish(str(exc))
        await order_cmd.finish(f"已挂：到价 {price:.1f}℃ 平仓。碰到这个价自动收手。")

    if action not in LONG_WORDS and action not in SHORT_WORDS:
        await order_cmd.finish(usage)
    side = Side.LONG if action in LONG_WORDS else Side.SHORT
    try:
        state = await engine.ensure_game(user_id, group_id, province)
    except GameError as exc:
        await order_cmd.finish(f"{exc}\n可用省份：{_province_list()}")
    try:
        amount = parse_amount(amount_text, state.account.cash)
    except AmountError as exc:
        await order_cmd.finish(str(exc))
    try:
        outcome = await engine.place_order(
            user_id, group_id, province, OrderKind.BET, side, price, stake=amount
        )
    except GameError as exc:
        await order_cmd.finish(str(exc))
    market = outcome.state.wxi
    await order_cmd.finish(
        f"已挂：到价 {price:.1f}℃ {_side_label(side)} {_money(outcome.order.stake)} · "
        f"{market.province_short}/{market.city_name}。碰到这个价自动买入。"
    )


@cancel_cmd.handle()
async def _(event: MessageEvent) -> None:
    user_id, group_id = _event_ids(event)
    try:
        count = await get_engine().cancel_orders(user_id, group_id)
    except GameError as exc:
        await cancel_cmd.finish(str(exc))
    await cancel_cmd.finish(f"已撤 {count} 个到价单" if count else "没有到价单")
