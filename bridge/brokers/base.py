from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# Anything not in here is treated as still working at the broker, including
# pending_cancel. An order isn't gone until the broker says it's gone.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


class BrokerError(Exception):
    pass


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str  # buy | sell
    qty: int
    client_order_id: str
    order_type: str = "market"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    type: str
    status: str
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    parent_id: str | None = None
    submitted_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.status not in TERMINAL_STATUSES


@dataclass
class Position:
    symbol: str
    qty: float  # signed, negative = short
    avg_price: float
    market_price: float | None = None

    @property
    def unrealized_pnl(self) -> float:
        if self.market_price is None:
            return 0.0
        return (self.market_price - self.avg_price) * self.qty


@dataclass(frozen=True)
class Fill:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    ts: datetime | None = field(compare=False, default=None)


class Broker(Protocol):
    name: str

    def submit(self, req: OrderRequest) -> Order: ...

    def cancel(self, order_id: str) -> bool:
        """Ask the broker to cancel. The return value is a request ack, not proof."""

    def open_orders(self, symbol: str | None = None) -> list[Order]: ...

    def positions(self) -> dict[str, Position]: ...

    def realized_pnl_today(self) -> float: ...
