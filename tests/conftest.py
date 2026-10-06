import itertools
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bridge.app import create_app
from bridge.brokers import PaperBroker
from bridge.config import Settings
from bridge.engine import Engine
from bridge.feed import FakeFeed
from bridge.store import Store

SECRET = "test-webhook-secret-123"
TOKEN = "test-dashboard-token-123"
# Wednesday, 10:00 in New York
MARKET_OPEN = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(MARKET_OPEN)


@pytest.fixture
def settings(tmp_path):
    return Settings(webhook_secret=SECRET, dashboard_token=TOKEN, db_path=str(tmp_path / "test.db"))


@pytest.fixture
def feed():
    return FakeFeed({"AAPL": 100.0, "MSFT": 400.0, "SPY": 500.0}, seed=1)


@pytest.fixture
def broker(feed, clock):
    b = PaperBroker(feed, slippage=0.0, clock=clock)
    b.attach(feed)
    return b


@pytest.fixture
def make_engine(settings, broker, clock):
    stores = []

    def make(settings=settings, broker=broker):
        store = Store(settings.db_path)
        stores.append(store)
        engine = Engine(settings, broker, store, clock=clock, cancel_confirm_delay=0, snapshot_ttl=0)
        if isinstance(broker, PaperBroker):
            broker.on_fill = engine.record_fill
        return engine

    yield make
    for s in stores:
        s.close()


@pytest.fixture
def engine(make_engine):
    return make_engine()


@pytest.fixture
def client_for():
    return lambda engine: TestClient(create_app(engine))


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as c:
        yield c


_ids = itertools.count(1)


def alert_payload(**overrides) -> dict:
    payload = {
        "secret": SECRET,
        "alert_id": f"test-{next(_ids)}",
        "symbol": "AAPL",
        "side": "buy",
        "qty": 10,
        "order_type": "market",
    }
    payload.update(overrides)
    return payload
