import logging
import re

from .brokers import PaperBroker
from .brokers.alpaca import AlpacaBroker
from .config import Settings
from .engine import Engine
from .feed import FakeFeed
from .notify import Notifier
from .store import Store

# Paper mode prices come from the fake feed, so it only knows these symbols.
PAPER_PRICES = {
    "SPY": 560.0, "QQQ": 480.0, "IWM": 220.0, "AAPL": 230.0, "MSFT": 420.0,
    "NVDA": 125.0, "AMZN": 190.0, "META": 560.0, "TSLA": 240.0, "AMD": 150.0,
}


def build(settings: Settings, feed: FakeFeed | None = None) -> tuple[Engine, FakeFeed | None]:
    store = Store(settings.db_path)
    notifier = Notifier(settings.telegram_bot_token, settings.telegram_chat_id)

    if settings.broker == "alpaca":
        broker = AlpacaBroker(settings.alpaca_key_id, settings.alpaca_secret_key, settings.alpaca_base_url)
        # Alpaca allows 200 requests a minute per account and a snapshot costs four.
        return Engine(settings, broker, store, notifier, snapshot_ttl=3.0), None

    feed = feed or FakeFeed(PAPER_PRICES)
    broker = PaperBroker(feed, tz=settings.timezone)
    broker.attach(feed)
    engine = Engine(settings, broker, store, notifier)
    broker.on_fill = engine.record_fill
    return engine, feed


class RedactTokens(logging.Filter):
    """uvicorn's access log prints the query string, which is where /events keeps its token."""

    pattern = re.compile(r"(token=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True
