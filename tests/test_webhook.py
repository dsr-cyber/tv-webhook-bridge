import json
from datetime import timedelta

from conftest import MARKET_OPEN, alert_payload


def test_valid_alert_places_order(client, engine, broker):
    r = client.post("/webhook", json=alert_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["order"]["client_order_id"].startswith("tvb-test-")
    assert broker.positions()["AAPL"].qty == 10
    assert engine.store.recent_decisions(1)[0]["outcome"] == "accepted"


def test_wrong_secret_is_401_and_touches_nothing(client, engine, broker):
    r = client.post("/webhook", json=alert_payload(secret="nope"))
    assert r.status_code == 401
    assert broker.positions() == {}
    assert engine.store.recent_decisions() == []


def test_missing_or_non_string_secret_is_401(client):
    payload = alert_payload()
    del payload["secret"]
    assert client.post("/webhook", json=payload).status_code == 401
    assert client.post("/webhook", json=alert_payload(secret=12345)).status_code == 401


def test_secret_check_is_constant_time(client, monkeypatch):
    import bridge.app

    calls = []
    real = bridge.app.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(bridge.app.hmac, "compare_digest", spy)
    client.post("/webhook", json=alert_payload(secret="guess"))
    assert calls == [(b"guess", b"test-webhook-secret-123")]


def test_text_plain_body_is_accepted(client):
    # what TradingView sends if the content-type sniffing goes the other way
    r = client.post("/webhook", content=json.dumps(alert_payload()), headers={"content-type": "text/plain"})
    assert r.json()["status"] == "accepted"


def test_non_json_body_is_400(client):
    assert client.post("/webhook", content="buy AAPL").status_code == 400
    assert client.post("/webhook", json=[1, 2]).status_code == 400


def test_invalid_alert_is_422_and_logged(client, engine):
    # an unexpanded TradingView placeholder is the usual culprit
    r = client.post("/webhook", json=alert_payload(qty="{{strategy.order.contracts}}"))
    assert r.status_code == 422
    assert any(d.startswith("qty") for d in r.json()["detail"])
    assert engine.store.recent_decisions(1)[0]["outcome"] == "invalid"


def test_duplicate_alert_id_trades_once(client, broker, engine):
    payload = alert_payload(alert_id="AAPL-5m-1759845600")
    first = client.post("/webhook", json=payload)
    second = client.post("/webhook", json=payload)
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert len(broker.fills) == 1
    assert broker.positions()["AAPL"].qty == 10


def test_refused_alert_id_stays_used(client, engine, broker):
    payload = alert_payload(qty=1000)
    assert client.post("/webhook", json=payload).json()["rule"] == "position_size"
    assert client.post("/webhook", json={**payload, "qty": 1}).json()["status"] == "duplicate"
    assert broker.fills == []


def test_stale_alert_is_refused(client, broker):
    old = (MARKET_OPEN - timedelta(seconds=120)).isoformat()
    r = client.post("/webhook", json=alert_payload(timestamp=old))
    assert r.json()["status"] == "refused" and r.json()["rule"] == "stale"
    assert broker.fills == []


def test_fresh_timestamp_passes(client):
    ts = (MARKET_OPEN - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    assert client.post("/webhook", json=alert_payload(timestamp=ts)).json()["status"] == "accepted"


def test_alert_without_timestamp_skips_age_check(client):
    assert client.post("/webhook", json=alert_payload()).json()["status"] == "accepted"


def test_broker_error_is_502(client, engine):
    r = client.post("/webhook", json=alert_payload(symbol="NVDA"))  # no price in the feed
    assert r.status_code == 502
    assert "no price" in r.json()["reason"]
    assert engine.store.recent_decisions(1)[0]["outcome"] == "error"


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "broker": "paper"}
