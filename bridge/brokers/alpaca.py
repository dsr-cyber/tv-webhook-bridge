"""Alpaca Trading API v2 adapter (REST, via httpx).

Field names follow docs.alpaca.markets as of October 2026. Prices and quantities
go over the wire as strings, which is what Alpaca's own examples do.
"""

import logging
from datetime import datetime

import httpx

from .base import BrokerError, Order, OrderRequest, Position

log = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"


def _price(p: float) -> str:
    # Alpaca rejects sub-penny prices for anything at or above $1.
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def order_body(req: OrderRequest) -> dict:
    body = {
        "symbol": req.symbol,
        "qty": str(req.qty),
        "side": req.side,
        "type": req.order_type,
        # day, not gtc: a limit entry from yesterday's signal shouldn't fill this
        # morning. The cost is that bracket legs also expire at the close.
        "time_in_force": "day",
        "client_order_id": req.client_order_id,
    }
    if req.order_type == "limit":
        body["limit_price"] = _price(req.limit_price)

    if req.stop_loss is not None and req.take_profit is not None:
        body["order_class"] = "bracket"
    elif req.stop_loss is not None or req.take_profit is not None:
        body["order_class"] = "oto"
    if req.take_profit is not None:
        body["take_profit"] = {"limit_price": _price(req.take_profit)}
    if req.stop_loss is not None:
        body["stop_loss"] = {"stop_price": _price(req.stop_loss)}
    return body


def _num(v) -> float | None:
    return None if v in (None, "") else float(v)


def _when(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _to_order(raw: dict, parent_id: str | None = None) -> Order:
    return Order(
        id=raw["id"],
        client_order_id=raw.get("client_order_id") or "",
        symbol=raw.get("symbol") or "",
        side=raw.get("side") or "",
        qty=_num(raw.get("qty")) or 0.0,
        type=raw.get("type") or raw.get("order_type") or "",
        status=raw.get("status") or "",
        limit_price=_num(raw.get("limit_price")),
        stop_price=_num(raw.get("stop_price")),
        filled_qty=_num(raw.get("filled_qty")) or 0.0,
        filled_avg_price=_num(raw.get("filled_avg_price")),
        parent_id=parent_id,
        submitted_at=_when(raw.get("submitted_at")),
    )


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, key_id: str, secret_key: str, base_url: str = PAPER_URL,
                 timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        if base_url.rstrip("/") != PAPER_URL:
            log.warning("Alpaca base URL is %s, not the paper endpoint", base_url)
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise BrokerError(f"alpaca {method} {path} failed: {type(e).__name__}") from e

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise BrokerError(f"alpaca {r.status_code}: {msg}")

    def submit(self, req: OrderRequest) -> Order:
        try:
            r = self._http.post("/v2/orders", json=order_body(req))
        except httpx.TransportError as e:
            # A timeout doesn't mean the order wasn't placed. Look for it by our own
            # id before reporting failure, so nobody re-sends a live order.
            found = self.find_by_client_order_id(req.client_order_id)
            if found is not None:
                log.warning("submit of %s raised %s but the order exists", req.client_order_id, type(e).__name__)
                return found
            raise BrokerError(
                f"order state unknown: submit raised {type(e).__name__} and "
                f"{req.client_order_id} isn't visible at Alpaca yet; check before re-sending"
            ) from e
        self._check(r)
        return _to_order(r.json())

    def find_by_client_order_id(self, client_order_id: str) -> Order | None:
        r = self._call("GET", "/v2/orders:by_client_order_id",
                       params={"client_order_id": client_order_id})
        if r.status_code == 404:
            return None
        self._check(r)
        return _to_order(r.json())

    def cancel(self, order_id: str) -> bool:
        r = self._call("DELETE", f"/v2/orders/{order_id}")
        if r.status_code in (200, 204):
            return True
        if r.status_code in (404, 422):  # gone, or no longer cancelable (likely filled)
            return False
        self._check(r)
        return False

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        params = {"status": "open", "limit": 500}
        if symbol:
            params["symbols"] = symbol
        r = self._call("GET", "/v2/orders", params=params)
        self._check(r)
        out = []
        for raw in r.json():
            out.append(_to_order(raw))
            # only present if the account defaults to nested; flatten either way
            out.extend(_to_order(leg, parent_id=raw["id"]) for leg in raw.get("legs") or [])
        return [o for o in out if o.is_live]

    def _raw_positions(self) -> list[dict]:
        r = self._call("GET", "/v2/positions")
        self._check(r)
        return r.json()

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._raw_positions():
            qty = abs(float(p["qty"]))
            if p.get("side") == "short":
                qty = -qty
            out[p["symbol"]] = Position(p["symbol"], qty, float(p["avg_entry_price"]),
                                        _num(p.get("current_price")))
        return out

    def realized_pnl_today(self) -> float:
        # Alpaca has no "realized today" field. equity - last_equity is the whole
        # day's change; take out today's move on open positions and what's left is
        # realized. A deposit or withdrawal today will throw this off.
        r = self._call("GET", "/v2/account")
        self._check(r)
        acct = r.json()
        day_change = float(acct["equity"]) - float(acct["last_equity"])
        open_change = sum(_num(p.get("unrealized_intraday_pl")) or 0.0 for p in self._raw_positions())
        return round(day_change - open_change, 2)
