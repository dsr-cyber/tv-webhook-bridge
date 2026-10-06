import re
from dataclasses import replace

from conftest import MARKET_OPEN, alert_payload

from bridge.engine import new_client_order_id
from bridge.feed import Bar


def post(client, **kw):
    return client.post("/webhook", json=alert_payload(**kw)).json()


def test_bracket_entry_rests_both_legs(client, broker):
    assert post(client, stop_loss=98.0, take_profit=104.0)["status"] == "accepted"
    legs = broker.open_orders("AAPL")
    assert sorted(o.type for o in legs) == ["limit", "stop"]


def test_exit_cancels_legs_then_flattens(client, broker):
    post(client, stop_loss=98.0, take_profit=104.0)
    r = post(client, side="sell")
    assert r["status"] == "accepted" and r["reason"].startswith("exit:")
    assert broker.open_orders() == []
    assert "AAPL" not in broker.positions()


def test_exit_is_sized_from_the_broker_position(client, broker):
    post(client, qty=10)
    r = post(client, side="sell", qty=50)
    assert "sell 10 AAPL" in r["reason"]
    assert "AAPL" not in broker.positions()  # flat, not short 40


class LegFillsDuringCancel:
    """The cancel call says yes, but the stop had already filled at the exchange."""

    name = "paper"

    def __init__(self, inner, feed):
        self._inner, self._feed = inner, feed

    def cancel(self, order_id):
        self._feed.push("AAPL", Bar(MARKET_OPEN, 97.0, 97.2, 96.8, 97.0))
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_exit_trusts_positions_not_the_cancel_ack(make_engine, client_for, broker, feed):
    racy = LegFillsDuringCancel(broker, feed)
    engine = make_engine(broker=racy)
    broker.on_fill = engine.record_fill
    client = client_for(engine)

    post(client, stop_loss=98.0, take_profit=104.0)
    r = post(client, side="sell")
    assert r["status"] == "refused" and r["rule"] == "already_flat"
    # the stop filled once; no second sell went out to turn us short
    assert [f.side for f in broker.fills] == ["buy", "sell"]
    assert "AAPL" not in broker.positions()


class CancelIgnored:
    name = "paper"

    def __init__(self, inner):
        self._inner = inner

    def cancel(self, order_id):
        return True  # "success", and nothing actually happens

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_exit_not_sent_while_orders_still_working(make_engine, client_for, broker):
    engine = make_engine(broker=CancelIgnored(broker))
    client = client_for(engine)
    post(client, stop_loss=98.0)
    r = post(client, side="sell")
    assert r["rule"] == "exit_blocked"
    assert broker.positions()["AAPL"].qty == 10
    assert len(broker.open_orders()) == 1


def test_client_order_ids_are_unique_and_safe():
    ids = {new_client_order_id("AAPL long {{time}}") for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(r"tvb-[A-Za-z0-9_-]+-[0-9a-f]{8}", i) and len(i) <= 128 for i in ids)


def test_every_order_gets_its_own_client_order_id(client, engine):
    for _ in range(3):
        post(client, qty=1)
    rows = engine.store._fetch("select client_order_id from orders")
    assert len({r[0] for r in rows}) == 3


def test_kill_switch_blocks_entries_but_not_exits(client, engine, broker):
    post(client, qty=5)
    engine.set_kill_switch(True)
    assert post(client, qty=5)["rule"] == "kill_switch"
    assert post(client, side="sell", qty=5)["status"] == "accepted"
    assert "AAPL" not in broker.positions()


def test_kill_switch_survives_restart(make_engine):
    make_engine().set_kill_switch(True)
    assert make_engine().kill_switch is True


def test_order_rate_limit(make_engine, client_for, settings):
    engine = make_engine(settings=replace(settings, max_orders_per_minute=2))
    client = client_for(engine)
    assert post(client, qty=1)["status"] == "accepted"
    assert post(client, qty=1)["status"] == "accepted"
    assert post(client, qty=1)["rule"] == "order_rate"


def test_daily_loss_cap_stops_new_entries(make_engine, client_for, settings, feed):
    engine = make_engine(settings=replace(settings, daily_loss_cap=50))
    client = client_for(engine)
    post(client, qty=20, stop_loss=98.0)
    feed.push("AAPL", Bar(MARKET_OPEN, 96.0, 96.5, 95.5, 96.0))  # gaps through the stop: -80
    r = post(client, qty=1)
    assert r["rule"] == "daily_loss" and "-80.00" in r["reason"]


def test_snapshot(client, engine):
    post(client, stop_loss=98.0, take_profit=104.0)
    snap = engine.snapshot()
    assert snap["broker"] == "paper"
    assert snap["positions"][0]["symbol"] == "AAPL"
    assert len(snap["orders"]) == 2
    assert snap["decisions"][0]["outcome"] == "accepted"
    assert set(snap["pnl"]) == {"realized", "unrealized", "total"}
