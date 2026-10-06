from datetime import timezone

import pytest
from pydantic import ValidationError

from bridge.config import Settings
from bridge.models import Alert

BASE = dict(secret="s", alert_id="x1", symbol="aapl", side="BUY", qty="10")


def test_tradingview_style_values_are_normalised():
    a = Alert(**{**BASE, "symbol": "NASDAQ:aapl"})
    assert (a.symbol, a.side, a.qty) == ("AAPL", "buy", 10)


def test_naive_timestamp_is_utc():
    a = Alert(**BASE, timestamp="2026-10-07T14:00:00")
    assert a.timestamp.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "extra",
    [
        {"qty": 0},
        {"side": "short"},
        {"order_type": "limit"},  # no limit_price
        {"limit_price": 100},  # limit_price on a market order
        {"stop_loss": 110, "take_profit": 100},  # inverted buy bracket
        {"order_type": "limit", "limit_price": 100, "stop_loss": 101},
        {"symbol": "AAPL; DROP"},
    ],
)
def test_rejects_bad_alerts(extra):
    with pytest.raises(ValidationError):
        Alert(**{**BASE, **extra})


def test_sell_bracket_is_mirrored():
    a = Alert(**{**BASE, "side": "sell", "stop_loss": 110, "take_profit": 90})
    assert a.has_bracket


def test_public_dict_drops_secret():
    assert "secret" not in Alert(**BASE).public_dict()


def test_settings_refuse_short_secrets():
    with pytest.raises(ValueError):
        Settings.from_env({"WEBHOOK_SECRET": "short", "DASHBOARD_TOKEN": "x" * 20})


def test_settings_pick_alpaca_only_with_keys():
    env = {"WEBHOOK_SECRET": "s" * 20, "DASHBOARD_TOKEN": "t" * 20}
    assert Settings.from_env(env).broker == "paper"
    keyed = {**env, "ALPACA_KEY_ID": "k", "ALPACA_SECRET_KEY": "v"}
    assert Settings.from_env(keyed).broker == "alpaca"
    with pytest.raises(ValueError):
        Settings.from_env({**env, "BROKER": "alpaca"})


def test_settings_allow_list_parsing():
    env = {"WEBHOOK_SECRET": "s" * 20, "DASHBOARD_TOKEN": "t" * 20, "ALLOWED_SYMBOLS": " spy, qqq ,,"}
    assert Settings.from_env(env).allowed_symbols == frozenset({"SPY", "QQQ"})


def test_settings_trading_days():
    env = {"WEBHOOK_SECRET": "s" * 20, "DASHBOARD_TOKEN": "t" * 20, "TRADING_DAYS": "Mon, Wed,friday"}
    assert Settings.from_env(env).trading_days == frozenset({0, 2, 4})
