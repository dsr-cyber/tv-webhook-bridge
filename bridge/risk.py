"""Pre-trade checks.

Every rule is a plain function of (alert, context, limits) that returns None when
the alert passes or a short human-readable reason when it doesn't. Nothing in here
talks to the broker or the database; the engine gathers the context first.
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

from .models import Alert


@dataclass(frozen=True)
class Limits:
    max_position_per_symbol: int = 100
    max_open_positions: int = 5
    daily_loss_cap: float = 500.0
    max_orders_per_minute: int = 10
    allow_short: bool = False
    trading_start: time = time(9, 35)
    trading_end: time = time(15, 50)
    timezone: str = "America/New_York"
    allowed_symbols: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, s) -> "Limits":
        return cls(
            max_position_per_symbol=s.max_position_per_symbol,
            max_open_positions=s.max_open_positions,
            daily_loss_cap=s.daily_loss_cap,
            max_orders_per_minute=s.max_orders_per_minute,
            allow_short=s.allow_short,
            trading_start=s.trading_start,
            trading_end=s.trading_end,
            timezone=s.timezone,
            allowed_symbols=s.allowed_symbols,
        )


@dataclass(frozen=True)
class Context:
    now: datetime  # tz-aware
    positions: dict[str, int] = field(default_factory=dict)  # symbol -> signed qty
    realized_pnl_today: float = 0.0
    orders_last_minute: int = 0
    kill_switch: bool = False


@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule: str = ""
    reason: str = ""


def is_exit(alert: Alert, ctx: Context) -> bool:
    """True when the alert trades against an existing position."""
    pos = ctx.positions.get(alert.symbol, 0)
    return (pos > 0 and alert.side == "sell") or (pos < 0 and alert.side == "buy")


def check_kill_switch(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    # Exits still go through. Blocking an exit is the last thing you want
    # right after somebody hit the kill switch.
    if ctx.kill_switch and not is_exit(alert, ctx):
        return "kill switch is on"
    return None


def check_symbol(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if limits.allowed_symbols and alert.symbol not in limits.allowed_symbols:
        return f"{alert.symbol} is not in the allow-list"
    return None


def check_window(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    local = ctx.now.astimezone(ZoneInfo(limits.timezone))
    if local.weekday() >= 5:
        return f"weekend ({local:%a %H:%M} {limits.timezone})"
    t = local.time()
    if not (limits.trading_start <= t < limits.trading_end):
        return (
            f"outside trading window {limits.trading_start:%H:%M}-{limits.trading_end:%H:%M} "
            f"(now {t:%H:%M} {limits.timezone})"
        )
    return None


def check_order_rate(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if ctx.orders_last_minute >= limits.max_orders_per_minute:
        return f"{ctx.orders_last_minute} orders in the last minute (limit {limits.max_orders_per_minute})"
    return None


def check_daily_loss(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if is_exit(alert, ctx):
        return None
    if ctx.realized_pnl_today <= -abs(limits.daily_loss_cap):
        return f"daily loss cap hit (realized {ctx.realized_pnl_today:.2f}, cap -{abs(limits.daily_loss_cap):.2f})"
    return None


def check_short(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if alert.side == "sell" and ctx.positions.get(alert.symbol, 0) <= 0 and not limits.allow_short:
        return f"no long {alert.symbol} position to sell and shorting is disabled"
    return None


def check_position_size(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if is_exit(alert, ctx):
        return None
    current = ctx.positions.get(alert.symbol, 0)
    after = abs(current) + alert.qty
    if after > limits.max_position_per_symbol:
        return f"{alert.symbol} position would be {after}, max is {limits.max_position_per_symbol}"
    return None


def check_open_positions(alert: Alert, ctx: Context, limits: Limits) -> str | None:
    if ctx.positions.get(alert.symbol, 0) != 0:
        return None  # adding to or exiting something we already hold
    open_count = sum(1 for q in ctx.positions.values() if q != 0)
    if open_count >= limits.max_open_positions:
        return f"{open_count} positions open, max is {limits.max_open_positions}"
    return None


Rule = Callable[[Alert, Context, Limits], "str | None"]

# Cheapest and most absolute first, so the logged reason is the most useful one.
RULES: list[tuple[str, Rule]] = [
    ("kill_switch", check_kill_switch),
    ("symbol", check_symbol),
    ("window", check_window),
    ("order_rate", check_order_rate),
    ("daily_loss", check_daily_loss),
    ("short", check_short),
    ("position_size", check_position_size),
    ("open_positions", check_open_positions),
]


def evaluate(alert: Alert, ctx: Context, limits: Limits) -> Verdict:
    for name, rule in RULES:
        reason = rule(alert, ctx, limits)
        if reason:
            return Verdict(False, name, reason)
    return Verdict(True)
