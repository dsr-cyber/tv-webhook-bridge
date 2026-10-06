"""In-memory simulated broker.

Fill rules, all chosen to understate results rather than flatter them:

- market orders fill at the last price plus/minus `slippage`
- a stop that the bar gaps through fills at the bar's open, not at the stop
  (min(stop, open) for a long's stop, max(stop, open) for a short's)
- if a bar touches both the stop and the target, the stop is assumed to have
  filled first; a bar doesn't tell you which came first
- a target fills at its limit price even when the bar opened through it
- bracket legs armed by an entry mid-bar are checked against that same bar's
  stop side only, since we don't know where in the bar the entry happened
"""

import itertools
import threading
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from ..feed import Bar
from .base import BrokerError, Fill, Order, OrderRequest, Position


class PriceSource(Protocol):
    def last_price(self, symbol: str) -> float | None: ...


class PaperBroker:
    name = "paper"

    def __init__(self, prices: PriceSource, slippage: float = 0.01,
                 on_fill: Callable[[Fill], None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 tz: str = "America/New_York"):
        self._prices = prices
        self._slip = slippage
        self._on_fill = on_fill
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = ZoneInfo(tz)
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._orders: dict[str, Order] = {}
        self._pending_brackets: dict[str, tuple[float | None, float | None]] = {}
        self._oco: dict[str, str] = {}
        self._positions: dict[str, Position] = {}
        self._realized: dict[date, float] = {}
        self._used_coids: set[str] = set()
        self.fills: list[Fill] = []

    def attach(self, feed) -> None:
        feed.subscribe(self.on_bar)

    def submit(self, req: OrderRequest) -> Order:
        with self._lock:
            if not req.client_order_id:
                raise BrokerError("client_order_id is required")
            if req.client_order_id in self._used_coids:
                raise BrokerError(f"client_order_id {req.client_order_id} already used")
            last = self._prices.last_price(req.symbol)
            if last is None:
                raise BrokerError(f"no price for {req.symbol}")
            self._used_coids.add(req.client_order_id)

            order = Order(
                id=f"P{next(self._ids)}", client_order_id=req.client_order_id,
                symbol=req.symbol, side=req.side, qty=req.qty, type=req.order_type,
                status="new", limit_price=req.limit_price, submitted_at=self._clock(),
            )
            self._orders[order.id] = order
            if req.stop_loss is not None or req.take_profit is not None:
                self._pending_brackets[order.id] = (req.stop_loss, req.take_profit)

            buy = req.side == "buy"
            if req.order_type == "market":
                self._fill(order, last + self._slip if buy else last - self._slip)
            elif buy and req.limit_price >= last:
                self._fill(order, min(req.limit_price, last + self._slip))
            elif not buy and req.limit_price <= last:
                self._fill(order, max(req.limit_price, last - self._slip))
            return replace(order)

    def cancel(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or not order.is_live:
                return False
            order.status = "canceled"
            self._pending_brackets.pop(order_id, None)
            sibling = self._orders.get(self._oco.get(order_id, ""))
            if sibling is not None and sibling.is_live:
                sibling.status = "canceled"
            return True

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        with self._lock:
            return [replace(o) for o in self._orders.values()
                    if o.is_live and (symbol is None or o.symbol == symbol)]

    def positions(self) -> dict[str, Position]:
        with self._lock:
            return {s: replace(p, market_price=self._prices.last_price(s))
                    for s, p in self._positions.items()}

    def realized_pnl_today(self) -> float:
        with self._lock:
            return round(self._realized.get(self._today(), 0.0), 2)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        with self._lock:
            armed_now: set[str] = set()
            for order in self._live(symbol, entries=True):
                if order.type != "limit":
                    continue
                if order.side == "buy" and bar.low <= order.limit_price:
                    armed_now |= self._fill(order, min(order.limit_price, bar.open))
                elif order.side == "sell" and bar.high >= order.limit_price:
                    armed_now |= self._fill(order, max(order.limit_price, bar.open))

            for parent_id in {o.parent_id for o in self._live(symbol, entries=False)}:
                legs = [o for o in self._live(symbol, entries=False) if o.parent_id == parent_id]
                stop = next((o for o in legs if o.type == "stop"), None)
                target = next((o for o in legs if o.type == "limit"), None)

                if stop is not None and self._stop_touched(stop, bar):
                    if stop.id in armed_now:
                        price = stop.stop_price
                    elif stop.side == "sell":
                        price = min(stop.stop_price, bar.open)
                    else:
                        price = max(stop.stop_price, bar.open)
                    self._fill(stop, price)
                elif target is not None and target.id not in armed_now and self._target_touched(target, bar):
                    self._fill(target, target.limit_price)

    def _live(self, symbol: str, entries: bool) -> list[Order]:
        return sorted(
            (o for o in self._orders.values()
             if o.symbol == symbol and o.is_live and (o.parent_id is None) == entries),
            key=lambda o: int(o.id[1:]),
        )

    @staticmethod
    def _stop_touched(o: Order, bar: Bar) -> bool:
        return bar.low <= o.stop_price if o.side == "sell" else bar.high >= o.stop_price

    @staticmethod
    def _target_touched(o: Order, bar: Bar) -> bool:
        return bar.high >= o.limit_price if o.side == "sell" else bar.low <= o.limit_price

    def _today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def _fill(self, order: Order, price: float) -> set[str]:
        """Fill the whole order. Returns ids of any bracket legs this armed."""
        price = round(price, 4)
        order.status = "filled"
        order.filled_qty = order.qty
        order.filled_avg_price = price
        self._apply_to_position(order.symbol, order.side, order.qty, price)

        sibling = self._orders.get(self._oco.get(order.id, ""))
        if sibling is not None and sibling.is_live:
            sibling.status = "canceled"

        fill = Fill(order.id, order.client_order_id, order.symbol, order.side,
                    order.qty, price, self._clock())
        self.fills.append(fill)
        if self._on_fill:
            self._on_fill(fill)
        return self._arm_bracket(order)

    def _arm_bracket(self, parent: Order) -> set[str]:
        stop_px, target_px = self._pending_brackets.pop(parent.id, (None, None))
        exit_side = "sell" if parent.side == "buy" else "buy"
        legs = []
        if stop_px is not None:
            legs.append(Order(
                id=f"P{next(self._ids)}", client_order_id=f"{parent.client_order_id}-sl",
                symbol=parent.symbol, side=exit_side, qty=parent.qty, type="stop",
                status="new", stop_price=stop_px, parent_id=parent.id,
                submitted_at=self._clock(),
            ))
        if target_px is not None:
            legs.append(Order(
                id=f"P{next(self._ids)}", client_order_id=f"{parent.client_order_id}-tp",
                symbol=parent.symbol, side=exit_side, qty=parent.qty, type="limit",
                status="new", limit_price=target_px, parent_id=parent.id,
                submitted_at=self._clock(),
            ))
        for leg in legs:
            self._orders[leg.id] = leg
        if len(legs) == 2:
            a, b = legs
            self._oco[a.id], self._oco[b.id] = b.id, a.id
        return {leg.id for leg in legs}

    def _apply_to_position(self, symbol: str, side: str, qty: float, price: float) -> None:
        signed = qty if side == "buy" else -qty
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(symbol, signed, price)
            return

        if (pos.qty > 0) == (signed > 0):
            total = abs(pos.qty) + qty
            pos.avg_price = (pos.avg_price * abs(pos.qty) + price * qty) / total
            pos.qty += signed
            return

        closing = min(qty, abs(pos.qty))
        direction = 1 if pos.qty > 0 else -1
        today = self._today()
        self._realized[today] = self._realized.get(today, 0.0) + (price - pos.avg_price) * closing * direction

        remaining = pos.qty + signed
        if remaining == 0:
            del self._positions[symbol]
        elif (remaining > 0) != (pos.qty > 0):
            self._positions[symbol] = Position(symbol, remaining, price)  # flipped
        else:
            pos.qty = remaining
