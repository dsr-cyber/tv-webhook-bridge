import itertools
from datetime import datetime, timezone

import pytest

from bridge.brokers import BrokerError, OrderRequest, PaperBroker
from bridge.feed import Bar, FakeFeed

T = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def feed():
    return FakeFeed({"AAPL": 100.0})


@pytest.fixture
def broker(feed):
    b = PaperBroker(feed, slippage=0.0, clock=lambda: T)
    b.attach(feed)
    return b


def bar(o, h, l, c):
    return Bar(T, o, h, l, c)


_ids = itertools.count(1)


def req(**kw):
    base = dict(symbol="AAPL", side="buy", qty=10, client_order_id=f"t{next(_ids)}")
    base.update(kw)
    return OrderRequest(**base)


def test_market_order_fills_at_last_plus_slippage(feed):
    b = PaperBroker(feed, slippage=0.05)
    o = b.submit(req())
    assert o.status == "filled" and o.filled_avg_price == pytest.approx(100.05)
    assert b.positions()["AAPL"].qty == 10


def test_unknown_symbol_is_rejected(broker):
    with pytest.raises(BrokerError):
        broker.submit(req(symbol="MSFT"))


def test_reused_client_order_id_is_rejected(broker):
    broker.submit(req(client_order_id="same"))
    with pytest.raises(BrokerError):
        broker.submit(req(client_order_id="same"))


def test_long_stop_gapped_through_fills_at_open(broker, feed):
    broker.submit(req(stop_loss=98.0))
    feed.push("AAPL", bar(96.5, 97.0, 96.0, 96.8))  # opens below the stop
    stop_fill = broker.fills[-1]
    assert stop_fill.side == "sell"
    assert stop_fill.price == 96.5  # not 98.0
    assert "AAPL" not in broker.positions()
    assert broker.realized_pnl_today() == pytest.approx(-35.0)


def test_long_stop_touched_intrabar_fills_at_stop(broker, feed):
    broker.submit(req(stop_loss=98.0))
    feed.push("AAPL", bar(99.5, 99.8, 97.5, 98.2))
    assert broker.fills[-1].price == 98.0


def test_short_stop_gapped_through_fills_at_open(feed):
    b = PaperBroker(feed, slippage=0.0, clock=lambda: T)
    b.attach(feed)
    b.submit(req(side="sell", stop_loss=102.0))
    feed.push("AAPL", bar(103.4, 104.0, 103.0, 103.5))
    assert b.fills[-1].side == "buy" and b.fills[-1].price == 103.4
    assert b.realized_pnl_today() == pytest.approx(-34.0)


def test_stop_wins_when_bar_touches_both_legs(broker, feed):
    broker.submit(req(stop_loss=98.0, take_profit=102.0))
    feed.push("AAPL", bar(100.0, 102.5, 97.5, 101.0))
    exits = broker.fills[1:]
    assert len(exits) == 1 and exits[0].price == 98.0
    assert broker.open_orders() == []  # target leg got cancelled with it


def test_target_fill_cancels_stop(broker, feed):
    broker.submit(req(stop_loss=98.0, take_profit=102.0))
    feed.push("AAPL", bar(100.5, 102.2, 100.1, 102.0))
    assert broker.fills[-1].price == 102.0
    assert broker.open_orders() == []
    assert broker.realized_pnl_today() == pytest.approx(20.0)


def test_target_does_not_get_gap_open_credit(broker, feed):
    broker.submit(req(take_profit=102.0))
    feed.push("AAPL", bar(104.0, 104.5, 103.5, 104.0))
    assert broker.fills[-1].price == 102.0


def test_bracket_legs_only_exist_after_parent_fills(broker, feed):
    parent = broker.submit(req(order_type="limit", limit_price=99.0, stop_loss=97.0, take_profit=103.0))
    assert parent.status == "new"
    assert [o.id for o in broker.open_orders()] == [parent.id]

    feed.push("AAPL", bar(99.8, 100.0, 98.9, 99.5))
    live = broker.open_orders()
    assert {o.type for o in live} == {"stop", "limit"}
    assert all(o.parent_id == parent.id and o.side == "sell" for o in live)
    assert broker.positions()["AAPL"].avg_price == 99.0


def test_limit_entry_gapping_below_gets_the_open(broker, feed):
    broker.submit(req(order_type="limit", limit_price=99.0))
    feed.push("AAPL", bar(98.0, 98.5, 97.9, 98.2))
    assert broker.fills[-1].price == 98.0


def test_entry_bar_checks_stop_but_not_target(broker, feed):
    broker.submit(req(order_type="limit", limit_price=99.0, stop_loss=98.0, take_profit=99.5))
    # fills the entry, then trades down through the stop and up through the target
    feed.push("AAPL", bar(99.6, 99.9, 97.8, 98.5))
    prices = [f.price for f in broker.fills]
    assert prices == [99.0, 98.0]


def test_cancelling_one_leg_cancels_the_other(broker):
    broker.submit(req(stop_loss=98.0, take_profit=102.0))
    leg = broker.open_orders()[0]
    assert broker.cancel(leg.id) is True
    assert broker.open_orders() == []
    assert broker.cancel(leg.id) is False


def test_cancelled_parent_never_arms_its_bracket(broker, feed):
    parent = broker.submit(req(order_type="limit", limit_price=95.0, stop_loss=94.0))
    broker.cancel(parent.id)
    feed.push("AAPL", bar(94.5, 95.0, 93.0, 93.5))
    assert broker.fills == [] and broker.open_orders() == []


def test_average_price_and_partial_close(broker, feed):
    broker.submit(req(qty=10))
    feed.push("AAPL", bar(100, 102, 100, 102))
    broker.submit(req(qty=10))
    assert broker.positions()["AAPL"].avg_price == pytest.approx(101.0)
    broker.submit(req(side="sell", qty=5))
    assert broker.positions()["AAPL"].qty == 15
    assert broker.realized_pnl_today() == pytest.approx(5.0)


def test_unrealized_uses_last_price(broker, feed):
    broker.submit(req(qty=10))
    feed.push("AAPL", bar(100, 101, 100, 100.7))
    assert broker.positions()["AAPL"].unrealized_pnl == pytest.approx(7.0)
