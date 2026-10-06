import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import risk
from .brokers.base import Broker, BrokerError, Fill, Order, OrderRequest
from .config import Settings
from .models import Alert
from .notify import Notifier
from .store import Store

log = logging.getLogger("bridge")


def new_client_order_id(alert_id: str) -> str:
    # Unique per order, never per strategy. A fixed tag looks harmless until the
    # broker starts rejecting every order after the first as a duplicate.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", alert_id)[:40] or "alert"
    return f"tvb-{safe}-{secrets.token_hex(4)}"


@dataclass
class Result:
    status: str  # accepted | refused | duplicate | error
    rule: str = ""
    reason: str = ""
    order: Order | None = None

    @property
    def http_status(self) -> int:
        return 502 if self.status == "error" else 200

    def to_dict(self) -> dict:
        out = {"status": self.status}
        if self.rule:
            out["rule"] = self.rule
        if self.reason:
            out["reason"] = self.reason
        if self.order is not None:
            out["order"] = {"id": self.order.id, "client_order_id": self.order.client_order_id,
                            "status": self.order.status}
        return out


class Engine:
    def __init__(self, settings: Settings, broker: Broker, store: Store,
                 notifier: Notifier | None = None,
                 clock: Callable[[], datetime] | None = None,
                 cancel_confirm_delay: float = 0.25,
                 snapshot_ttl: float = 1.0):
        self.settings = settings
        self.limits = risk.Limits.from_settings(settings)
        self.broker = broker
        self.store = store
        self.notifier = notifier or Notifier()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._confirm_delay = cancel_confirm_delay
        self._snapshot_ttl = snapshot_ttl
        # Alerts are processed one at a time. Two alerts arriving together would
        # otherwise both read the same position and both pass the size check.
        self._lock = threading.Lock()
        self._snap_lock = threading.Lock()
        self._snap: tuple[float, dict] | None = None

    @property
    def kill_switch(self) -> bool:
        return self.store.get_flag("kill_switch", "0") == "1"

    def set_kill_switch(self, on: bool, source: str = "dashboard") -> None:
        self.store.set_flag("kill_switch", "1" if on else "0")
        state = "ON" if on else "off"
        log.warning("kill switch %s (%s)", state, source)
        self.store.add_decision(self._clock(), "kill_switch", reason=f"kill switch {state} via {source}")
        self.notifier.send(f"Kill switch {state} via {source}")
        self._snap = None

    def record_fill(self, fill: Fill) -> None:
        self.store.add_fill(fill)
        log.info("fill %s %s %s @ %s (%s)", fill.side, fill.qty, fill.symbol, fill.price, fill.client_order_id)
        self._snap = None

    def record_invalid(self, alert_id: str | None, reason: str) -> None:
        log.info("invalid alert %s: %s", alert_id, reason)
        self.store.add_decision(self._clock(), "invalid", alert_id=alert_id, reason=reason)

    def handle(self, alert: Alert) -> Result:
        now = self._clock()
        if not self.store.claim_alert(alert.alert_id, alert.public_dict(), now):
            log.info("duplicate alert %s ignored", alert.alert_id)
            self._decide(alert, "duplicate", reason="alert_id already processed")
            return Result("duplicate", reason="alert_id already processed")

        if alert.timestamp is not None:
            age = (now - alert.timestamp).total_seconds()
            if abs(age) > self.settings.max_alert_age_s:
                return self._refuse(alert, "stale", f"alert timestamp is {age:.0f}s off (max {self.settings.max_alert_age_s}s)")

        with self._lock:
            try:
                result = self._process(alert, now)
            except BrokerError as e:
                log.error("broker error on %s: %s", alert.alert_id, e)
                result = self._error(alert, str(e))
            except Exception as e:
                log.exception("unexpected error on %s", alert.alert_id)
                result = self._error(alert, f"{type(e).__name__}: {e}")
        self._snap = None
        return result

    def _error(self, alert: Alert, message: str) -> Result:
        message = message[:300]
        self._decide(alert, "error", rule="broker", reason=message)
        self.store.set_alert_outcome(alert.alert_id, "error")
        self.notifier.send(f"ERROR {alert.side} {alert.qty} {alert.symbol}: {message}")
        return Result("error", rule="broker", reason=message)

    def _process(self, alert: Alert, now: datetime) -> Result:
        held = {s: p.qty for s, p in self.broker.positions().items()}
        ctx = risk.Context(
            now=now,
            positions=held,
            realized_pnl_today=self.broker.realized_pnl_today(),
            orders_last_minute=self.store.orders_since(now - timedelta(seconds=60)),
            kill_switch=self.kill_switch,
        )
        verdict = risk.evaluate(alert, ctx, self.limits)
        if not verdict.ok:
            return self._refuse(alert, verdict.rule, verdict.reason)
        if risk.is_exit(alert, ctx):
            return self._exit(alert)
        return self._submit(alert, alert.qty, with_bracket=True, note="entry")

    def _exit(self, alert: Alert) -> Result:
        still_working = self._cancel_and_confirm(alert.symbol)
        if still_working:
            ids = ", ".join(o.id for o in still_working)
            return self._refuse(alert, "exit_blocked",
                                f"orders still working after cancel ({ids}); exit not sent")

        # Size the exit from what the broker holds after the cancels, not from what
        # we read before them. A stop leg can fill while its cancel is in flight.
        pos = self.broker.positions().get(alert.symbol)
        held = pos.qty if pos else 0
        if held == 0:
            return self._refuse(alert, "already_flat",
                                f"{alert.symbol} is flat after cancelling; a bracket leg filled first")
        if (held > 0) != (alert.side == "sell"):
            return self._refuse(alert, "position_changed", f"{alert.symbol} position is now {held}")

        qty = int(min(alert.qty, abs(held)))
        note = "exit" if qty == abs(held) else f"partial exit, {abs(held) - qty} left without a stop"
        # TODO: re-place a protective stop for the remainder of a partial exit.
        return self._submit(alert, qty, with_bracket=False, note=note)

    def _cancel_and_confirm(self, symbol: str, attempts: int = 3) -> list[Order]:
        for attempt in range(attempts):
            working = self.broker.open_orders(symbol)
            if not working:
                return []
            for o in working:
                # The return value is only "request accepted". Proof is the next open_orders().
                self.broker.cancel(o.id)
            if self._confirm_delay:
                time.sleep(self._confirm_delay * (attempt + 1))
        return self.broker.open_orders(symbol)

    def _submit(self, alert: Alert, qty: int, with_bracket: bool, note: str) -> Result:
        req = OrderRequest(
            symbol=alert.symbol,
            side=alert.side,
            qty=qty,
            client_order_id=new_client_order_id(alert.alert_id),
            order_type=alert.order_type,
            limit_price=alert.limit_price,
            stop_loss=alert.stop_loss if with_bracket else None,
            take_profit=alert.take_profit if with_bracket else None,
        )
        order = self.broker.submit(req)
        self.store.add_order(self._clock(), alert.alert_id, req, order)
        reason = f"{note}: {req.side} {qty} {req.symbol} {req.order_type} -> {order.status} ({order.id})"
        log.info("accepted %s %s", alert.alert_id, reason)
        self._decide(alert, "accepted", reason=reason, qty=qty)
        self.store.set_alert_outcome(alert.alert_id, "accepted")
        self.notifier.send(reason)
        return Result("accepted", reason=reason, order=order)

    def _refuse(self, alert: Alert, rule: str, reason: str) -> Result:
        log.info("refused %s %s %s %s [%s]: %s", alert.alert_id, alert.side, alert.qty, alert.symbol, rule, reason)
        self._decide(alert, "refused", rule=rule, reason=reason)
        self.store.set_alert_outcome(alert.alert_id, f"refused:{rule}")
        self.notifier.send(f"Refused {alert.side} {alert.qty} {alert.symbol}: {reason}")
        return Result("refused", rule=rule, reason=reason)

    def _decide(self, alert: Alert, outcome: str, rule: str = "", reason: str = "", qty: int | None = None) -> None:
        self.store.add_decision(
            self._clock(), outcome, alert_id=alert.alert_id, symbol=alert.symbol,
            side=alert.side, qty=alert.qty if qty is None else qty, rule=rule, reason=reason,
        )

    def snapshot(self) -> dict:
        """Dashboard state. Cached briefly so several open tabs don't multiply broker calls."""
        with self._snap_lock:
            cached = self._snap
            if cached is not None and time.monotonic() - cached[0] < self._snapshot_ttl:
                return cached[1]
            snap = self._build_snapshot()
            self._snap = (time.monotonic(), snap)
            return snap

    def _build_snapshot(self) -> dict:
        positions = self.broker.positions()
        orders = self.broker.open_orders()
        realized = self.broker.realized_pnl_today()
        unrealized = sum(p.unrealized_pnl for p in positions.values())
        return {
            "broker": self.broker.name,
            "kill_switch": self.kill_switch,
            "pnl": {"realized": round(realized, 2), "unrealized": round(unrealized, 2),
                    "total": round(realized + unrealized, 2)},
            "positions": [
                {"symbol": p.symbol, "qty": p.qty, "avg_price": round(p.avg_price, 4),
                 "last": p.market_price, "unrealized": round(p.unrealized_pnl, 2)}
                for p in sorted(positions.values(), key=lambda p: p.symbol)
            ],
            "orders": [
                {"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "type": o.type,
                 "limit_price": o.limit_price, "stop_price": o.stop_price, "status": o.status,
                 "parent_id": o.parent_id}
                for o in orders
            ],
            "decisions": self.store.recent_decisions(20),
        }
