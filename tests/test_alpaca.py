import json

import httpx
import pytest

from bridge.brokers import BrokerError, OrderRequest
from bridge.brokers.alpaca import PAPER_URL, AlpacaBroker, order_body

ORDER_JSON = {
    "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
    "client_order_id": "tvb-x-0000abcd",
    "symbol": "AAPL",
    "side": "buy",
    "qty": "10",
    "type": "market",
    "status": "accepted",
    "limit_price": None,
    "stop_price": None,
    "filled_qty": "0",
    "filled_avg_price": None,
    "submitted_at": "2026-10-07T14:00:00.123456Z",
}


class Recorder:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_broker(handler):
    rec = Recorder(handler)
    return AlpacaBroker("KEYID", "SECRET", transport=httpx.MockTransport(rec)), rec


def req(**kw):
    base = dict(symbol="AAPL", side="buy", qty=10, client_order_id="tvb-x-0000abcd")
    base.update(kw)
    return OrderRequest(**base)


def test_bracket_body_matches_alpaca_v2():
    body = order_body(req(order_type="limit", limit_price=189.5, stop_loss=185.0, take_profit=196.25))
    assert body == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "client_order_id": "tvb-x-0000abcd",
        "limit_price": "189.50",
        "order_class": "bracket",
        "take_profit": {"limit_price": "196.25"},
        "stop_loss": {"stop_price": "185.00"},
    }


def test_stop_only_is_oto():
    body = order_body(req(stop_loss=185.0))
    assert body["order_class"] == "oto"
    assert "take_profit" not in body


def test_plain_market_order_has_no_order_class():
    body = order_body(req())
    assert "order_class" not in body and "limit_price" not in body


def test_prices_are_rounded_to_valid_ticks():
    assert order_body(req(order_type="limit", limit_price=12.3456))["limit_price"] == "12.35"
    assert order_body(req(order_type="limit", limit_price=0.51234))["limit_price"] == "0.5123"


def test_submit_sends_auth_headers_and_parses_order():
    broker, rec = make_broker(lambda r: httpx.Response(200, json=ORDER_JSON))
    order = broker.submit(req(stop_loss=185.0, take_profit=196.0))

    sent = rec.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == f"{PAPER_URL}/v2/orders"
    assert sent.headers["APCA-API-KEY-ID"] == "KEYID"
    assert sent.headers["APCA-API-SECRET-KEY"] == "SECRET"
    assert json.loads(sent.content)["order_class"] == "bracket"
    assert (order.id, order.status, order.qty) == (ORDER_JSON["id"], "accepted", 10.0)


def test_submit_error_surfaces_alpaca_message():
    broker, _ = make_broker(lambda r: httpx.Response(403, json={"code": 40310000, "message": "insufficient buying power"}))
    with pytest.raises(BrokerError, match="insufficient buying power"):
        broker.submit(req())


def test_submit_timeout_finds_the_order_by_client_id():
    def handler(r):
        if r.method == "POST":
            raise httpx.ReadTimeout("slow", request=r)
        return httpx.Response(200, json={**ORDER_JSON, "status": "filled"})

    broker, rec = make_broker(handler)
    order = broker.submit(req())
    lookup = rec.requests[1]
    assert lookup.url.path == "/v2/orders:by_client_order_id"
    assert lookup.url.params["client_order_id"] == "tvb-x-0000abcd"
    assert order.status == "filled"


def test_submit_timeout_with_no_order_is_reported_as_unknown():
    def handler(r):
        if r.method == "POST":
            raise httpx.ConnectTimeout("down", request=r)
        return httpx.Response(404, json={"message": "order not found"})

    broker, _ = make_broker(handler)
    with pytest.raises(BrokerError, match="state unknown"):
        broker.submit(req())


@pytest.mark.parametrize("status, expected", [(204, True), (422, False), (404, False)])
def test_cancel(status, expected):
    broker, rec = make_broker(lambda r: httpx.Response(status))
    assert broker.cancel("abc-123") is expected
    assert rec.requests[0].method == "DELETE"
    assert rec.requests[0].url.path == "/v2/orders/abc-123"


def test_cancel_server_error_raises():
    broker, _ = make_broker(lambda r: httpx.Response(500, text="oops"))
    with pytest.raises(BrokerError):
        broker.cancel("abc")


def test_open_orders_query_and_legs():
    parent = {**ORDER_JSON, "status": "filled", "legs": [
        {**ORDER_JSON, "id": "leg-tp", "side": "sell", "type": "limit", "limit_price": "196", "status": "new"},
        {**ORDER_JSON, "id": "leg-sl", "side": "sell", "type": "stop", "stop_price": "185", "status": "held"},
    ]}
    broker, rec = make_broker(lambda r: httpx.Response(200, json=[parent]))
    orders = broker.open_orders("AAPL")

    params = rec.requests[0].url.params
    assert (params["status"], params["symbols"]) == ("open", "AAPL")
    assert [o.id for o in orders] == ["leg-tp", "leg-sl"]  # filled parent dropped
    assert all(o.parent_id == ORDER_JSON["id"] for o in orders)
    assert orders[1].stop_price == 185.0


def test_positions_are_signed():
    rows = [
        {"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "190.1", "current_price": "191"},
        {"symbol": "TSLA", "qty": "-5", "side": "short", "avg_entry_price": "250", "current_price": "248"},
    ]
    broker, _ = make_broker(lambda r: httpx.Response(200, json=rows))
    pos = broker.positions()
    assert pos["AAPL"].qty == 10 and pos["TSLA"].qty == -5
    assert pos["TSLA"].unrealized_pnl == pytest.approx(10.0)


def test_realized_pnl_today_backs_out_open_positions():
    def handler(r):
        if r.url.path == "/v2/account":
            return httpx.Response(200, json={"equity": "100150.00", "last_equity": "100000.00"})
        return httpx.Response(200, json=[
            {"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "190",
             "current_price": "196", "unrealized_intraday_pl": "60"},
        ])

    broker, _ = make_broker(handler)
    assert broker.realized_pnl_today() == pytest.approx(90.0)


def test_network_error_becomes_broker_error():
    def handler(r):
        raise httpx.ConnectError("no route", request=r)

    broker, _ = make_broker(handler)
    with pytest.raises(BrokerError, match="ConnectError"):
        broker.positions()
