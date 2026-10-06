from datetime import datetime, time, timezone

import pytest

from bridge import risk
from bridge.models import Alert
from bridge.risk import Context, Limits

# Wednesday 2026-10-07 14:00 UTC = 10:00 New York (EDT)
OPEN = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


def alert(**kw) -> Alert:
    base = dict(secret="x", alert_id="a1", symbol="AAPL", side="buy", qty=10)
    base.update(kw)
    return Alert(**base)


def ctx(**kw) -> Context:
    kw.setdefault("now", OPEN)
    return Context(**kw)


def test_clean_alert_passes():
    assert risk.evaluate(alert(), ctx(), Limits()).ok


def test_kill_switch_blocks_entries():
    v = risk.evaluate(alert(), ctx(kill_switch=True), Limits())
    assert not v.ok and v.rule == "kill_switch"


def test_kill_switch_lets_exits_through():
    v = risk.evaluate(alert(side="sell"), ctx(kill_switch=True, positions={"AAPL": 10}), Limits())
    assert v.ok


def test_allow_list():
    limits = Limits(allowed_symbols=frozenset({"SPY", "QQQ"}))
    v = risk.evaluate(alert(symbol="TSLA"), ctx(), limits)
    assert v.rule == "symbol" and "TSLA" in v.reason
    assert risk.evaluate(alert(symbol="SPY"), ctx(), limits).ok


def test_empty_allow_list_means_everything():
    assert risk.check_symbol(alert(symbol="ZZZ"), ctx(), Limits()) is None


@pytest.mark.parametrize(
    "now, ok",
    [
        (datetime(2026, 10, 7, 13, 34, tzinfo=timezone.utc), False),  # 09:34 ET
        (datetime(2026, 10, 7, 13, 35, tzinfo=timezone.utc), True),  # 09:35 ET
        (datetime(2026, 10, 7, 19, 49, tzinfo=timezone.utc), True),  # 15:49 ET
        (datetime(2026, 10, 7, 19, 50, tzinfo=timezone.utc), False),  # 15:50 ET
        (datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), False),  # Saturday
    ],
)
def test_trading_window(now, ok):
    assert (risk.check_window(alert(), ctx(now=now), Limits()) is None) is ok


def test_window_follows_dst():
    # 14:00 UTC is 09:00 in January (EST), before the open
    winter = datetime(2026, 1, 14, 14, 0, tzinfo=timezone.utc)
    assert risk.check_window(alert(), ctx(now=winter), Limits()) is not None


def test_trading_days_are_configurable():
    saturday = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    assert risk.check_window(alert(), ctx(now=saturday), Limits()) == "not a trading day (Sat America/New_York)"
    assert risk.check_window(alert(), ctx(now=saturday), Limits(trading_days=frozenset(range(7)))) is None


def test_custom_window():
    limits = Limits(trading_start=time(0, 0), trading_end=time(23, 59))
    assert risk.check_window(alert(), ctx(now=datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)), limits) is None


def test_order_rate():
    limits = Limits(max_orders_per_minute=3)
    assert risk.check_order_rate(alert(), ctx(orders_last_minute=2), limits) is None
    assert risk.check_order_rate(alert(), ctx(orders_last_minute=3), limits) is not None


def test_daily_loss_cap_blocks_new_risk_only():
    limits = Limits(daily_loss_cap=500)
    assert risk.check_daily_loss(alert(), ctx(realized_pnl_today=-499.99), limits) is None
    assert risk.check_daily_loss(alert(), ctx(realized_pnl_today=-500), limits) is not None
    exit_ctx = ctx(realized_pnl_today=-900, positions={"AAPL": 5})
    assert risk.check_daily_loss(alert(side="sell"), exit_ctx, limits) is None


def test_max_position_per_symbol():
    limits = Limits(max_position_per_symbol=100)
    assert risk.check_position_size(alert(qty=100), ctx(), limits) is None
    assert risk.check_position_size(alert(qty=101), ctx(), limits) is not None
    v = risk.check_position_size(alert(qty=30), ctx(positions={"AAPL": 80}), limits)
    assert v == "AAPL position would be 110, max is 100"


def test_max_position_ignores_exits():
    limits = Limits(max_position_per_symbol=10)
    assert risk.check_position_size(alert(side="sell", qty=50), ctx(positions={"AAPL": 50}), limits) is None


def test_max_open_positions():
    limits = Limits(max_open_positions=2)
    full = {"SPY": 5, "QQQ": -3, "IWM": 0}
    assert risk.check_open_positions(alert(), ctx(positions=full), limits) is not None
    # adding to something already open doesn't count as a new position
    assert risk.check_open_positions(alert(symbol="SPY"), ctx(positions=full), limits) is None


def test_shorting_disabled_by_default():
    v = risk.evaluate(alert(side="sell"), ctx(), Limits())
    assert v.rule == "short"
    assert risk.evaluate(alert(side="sell"), ctx(), Limits(allow_short=True)).ok


def test_first_failing_rule_wins():
    v = risk.evaluate(
        alert(symbol="TSLA", qty=10_000),
        ctx(kill_switch=True),
        Limits(allowed_symbols=frozenset({"SPY"})),
    )
    assert v.rule == "kill_switch"


def test_is_exit():
    assert risk.is_exit(alert(side="sell"), ctx(positions={"AAPL": 1}))
    assert risk.is_exit(alert(side="buy"), ctx(positions={"AAPL": -1}))
    assert not risk.is_exit(alert(side="buy"), ctx(positions={"AAPL": 1}))
    assert not risk.is_exit(alert(side="sell"), ctx())
