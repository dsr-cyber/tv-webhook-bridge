import logging
from dataclasses import replace

from bridge.brokers import PaperBroker
from bridge.brokers.alpaca import AlpacaBroker
from bridge.server import RedactTokens, build


def test_build_paper(settings):
    engine, feed = build(settings)
    assert isinstance(engine.broker, PaperBroker) and feed is not None
    assert engine.broker.on_fill == engine.record_fill
    engine.store.close()


def test_build_alpaca_has_no_feed(settings):
    engine, feed = build(replace(settings, broker="alpaca", alpaca_key_id="k", alpaca_secret_key="s"))
    assert isinstance(engine.broker, AlpacaBroker) and feed is None
    engine.broker.close()
    engine.store.close()


def test_access_log_hides_dashboard_token():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s"', (
        "127.0.0.1:5000", "GET", "/events?token=hunter2-hunter2&x=1"), None)
    RedactTokens().filter(record)
    assert "hunter2" not in record.getMessage()
    assert "token=***&x=1" in record.getMessage()
