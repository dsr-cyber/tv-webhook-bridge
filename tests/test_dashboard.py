import asyncio
import json

from conftest import TOKEN, alert_payload

from bridge.app import sse_events

AUTH = {"X-Dashboard-Token": TOKEN}


def test_index_and_static_are_served(client):
    assert "tv-webhook-bridge" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_state_needs_token(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    assert client.get("/api/state", headers=AUTH).json()["broker"] == "paper"


def test_kill_switch_toggle(client, engine):
    assert client.post("/kill", json={"on": True}).status_code == 401
    assert client.post("/kill", json={"on": "yes"}, headers=AUTH).status_code == 400

    assert client.post("/kill", json={"on": True}, headers=AUTH).json() == {"kill_switch": True}
    assert engine.kill_switch is True
    r = client.post("/webhook", json=alert_payload())
    assert r.json()["rule"] == "kill_switch"

    client.post("/kill", json={"on": False}, headers=AUTH)
    assert client.post("/webhook", json=alert_payload()).json()["status"] == "accepted"


def test_events_needs_token(client):
    assert client.get("/events").status_code == 401


# TestClient can't close an endless stream, so the generator is tested on its own.

def _take(gen, n):
    async def run():
        return [await anext(gen) for _ in range(n)]
    return asyncio.run(run())


async def _connected():
    return False


def test_sse_sends_only_changes():
    snaps = iter([{"a": 1}, {"a": 1}, {"a": 2}])

    async def get():
        return next(snaps)

    frames = _take(sse_events(get, _connected, interval=0, keepalive_every=100), 2)
    assert frames == ['data: {"a": 1}\n\n', 'data: {"a": 2}\n\n']


def test_sse_keepalive_when_idle():
    async def get():
        return {"same": True}

    frames = _take(sse_events(get, _connected, interval=0, keepalive_every=3), 2)
    assert frames[1] == ": keepalive\n\n"


def test_sse_frame_carries_engine_snapshot(client, engine):
    client.post("/webhook", json=alert_payload())

    async def get():
        return engine.snapshot()

    frame = _take(sse_events(get, _connected, interval=0), 1)[0]
    snap = json.loads(frame.removeprefix("data: "))
    assert snap["positions"][0]["symbol"] == "AAPL"
