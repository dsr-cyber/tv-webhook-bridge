"""A fake bar feed for the paper broker and the demo.

Random walk with the occasional gap at the open of a bar, because a feed that
never gaps hides exactly the stop-fill bug the paper broker exists to model.
"""

import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


BarCallback = Callable[[str, Bar], None]


class FakeFeed:
    def __init__(self, start_prices: dict[str, float], seed: int | None = None,
                 vol: float = 0.002, gap_chance: float = 0.03, gap_size: float = 0.01):
        self._last = dict(start_prices)
        self._rng = random.Random(seed)
        self._vol = vol
        self._gap_chance = gap_chance
        self._gap_size = gap_size
        self._subscribers: list[BarCallback] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: BarCallback) -> None:
        self._subscribers.append(cb)

    def symbols(self) -> list[str]:
        return list(self._last)

    def last_price(self, symbol: str) -> float | None:
        return self._last.get(symbol)

    def push(self, symbol: str, bar: Bar) -> None:
        with self._lock:
            self._last[symbol] = bar.close
        for cb in self._subscribers:
            cb(symbol, bar)

    def next_bar(self, symbol: str) -> Bar:
        prev = self._last[symbol]
        o = prev
        if self._rng.random() < self._gap_chance:
            o = prev * (1 + self._rng.choice((-1, 1)) * self._gap_size)
        px, hi, lo = o, o, o
        for _ in range(12):
            px *= 1 + self._rng.gauss(0, self._vol)
            hi, lo = max(hi, px), min(lo, px)
        return Bar(datetime.now(timezone.utc), round(o, 2), round(hi, 2), round(lo, 2), round(px, 2))

    def step(self) -> None:
        for sym in self.symbols():
            self.push(sym, self.next_bar(sym))

    def run(self, interval: float, stop: threading.Event) -> None:
        while not stop.wait(interval):
            self.step()
