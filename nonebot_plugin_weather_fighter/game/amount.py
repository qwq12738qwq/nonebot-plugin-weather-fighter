from __future__ import annotations

UNITS: tuple[tuple[str, float], ...] = (
    ("亿", 100_000_000.0),
    ("万", 10_000.0),
    ("w", 10_000.0),
    ("W", 10_000.0),
    ("千", 1_000.0),
    ("k", 1_000.0),
    ("K", 1_000.0),
)
ALL_IN = {"全", "全仓", "梭哈", "all", "ALL"}


class AmountError(ValueError):
    pass


def parse_amount(text: str | None, cash: float, *, default_pct: float = 20.0) -> float:
    """把“1000 / 1.5万 / 30% / 全”解析成投入金额，并校验不超过余额。"""
    raw = (text or "").strip().replace(",", "").replace("，", "").replace("元", "")
    if not raw:
        amount = cash * default_pct / 100.0
    elif raw in ALL_IN:
        amount = cash
    elif raw.endswith("%"):
        try:
            percent = float(raw[:-1])
        except ValueError as exc:
            raise AmountError("金额格式不对，例如：1000 / 1万 / 30% / 全") from exc
        amount = cash * percent / 100.0
    else:
        scale = 1.0
        for unit, multiplier in UNITS:
            if raw.endswith(unit):
                raw = raw[: -len(unit)]
                scale = multiplier
                break
        try:
            amount = float(raw) * scale
        except ValueError as exc:
            raise AmountError("金额格式不对，例如：1000 / 1万 / 30% / 全") from exc
    if amount <= 0:
        raise AmountError("还有反向消费?")
    if amount > cash + 1e-6:
        raise AmountError(f"穷了,只有 {cash:,.0f}")
    return amount


def parse_shares(text: str | None) -> int | None:
    """识别「N份」；不是份数表达时返回 None，交给 parse_amount 按金额处理。"""
    raw = (text or "").strip().replace(",", "").replace("，", "").replace("元", "")
    if not raw.endswith("份"):
        return None
    try:
        qty = int(float(raw[:-1].strip()))
    except ValueError as exc:
        raise AmountError("份数格式不对，例如：500份 / 1000 / 30% / 全") from exc
    if qty < 1:
        raise AmountError("至少 1 份")
    return qty
