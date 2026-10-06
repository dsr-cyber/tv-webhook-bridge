"""Run the bridge on the paper broker and throw a few alerts at it.

    python -m bridge.demo                 # then open http://127.0.0.1:8000
    python -m bridge.demo --duration 20   # exit on its own
"""

import argparse
import logging
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

import httpx
import uvicorn

from .app import create_app
from .config import Settings
from .feed import FakeFeed
from .server import build

SECRET = "demo-secret-not-for-real-use"
TOKEN = "demo-dashboard-token"
PRICES = {"AAPL": 230.0, "MSFT": 420.0, "SPY": 560.0, "QQQ": 480.0}


def demo_settings(db_path: str) -> Settings:
    return Settings(
        webhook_secret=SECRET,
        dashboard_token=TOKEN,
        db_path=db_path,
        broker="paper",
        max_position_per_symbol=200,
        max_open_positions=3,
        allowed_symbols=frozenset(PRICES),
        # open around the clock so the demo works whenever you run it
        trading_start=dtime(0, 0),
        trading_end=dtime.max,
        trading_days=frozenset(range(7)),
    )


def sample_alerts(feed: FakeFeed) -> list[tuple[str, dict]]:
    aapl, msft, qqq = (feed.last_price(s) for s in ("AAPL", "MSFT", "QQQ"))
    now = datetime.now(timezone.utc)

    def alert(alert_id, **kw):
        return {"secret": SECRET, "alert_id": alert_id, "order_type": "market",
                "timestamp": now.isoformat(), **kw}

    entry = alert("AAPL-5m-1", symbol="AAPL", side="buy", qty=50,
                  stop_loss=round(aapl * 0.98, 2), take_profit=round(aapl * 1.03, 2))
    return [
        ("buy 50 AAPL with a bracket", entry),
        ("same alert delivered twice", entry),
        ("buy 30 QQQ, stop only", alert("QQQ-5m-1", symbol="QQQ", side="buy", qty=30,
                                         stop_loss=round(qqq * 0.985, 2))),
        ("buy 20 MSFT, limit under market", alert("MSFT-5m-1", symbol="MSFT", side="buy", qty=20,
                                                  order_type="limit", limit_price=round(msft * 0.995, 2),
                                                  stop_loss=round(msft * 0.98, 2))),
        ("buy 500 SPY, over the size limit", alert("SPY-5m-1", symbol="SPY", side="buy", qty=500)),
        ("buy 10 TSLA, not on the allow-list", alert("TSLA-5m-1", symbol="TSLA", side="buy", qty=10)),
        ("alert fired 2 minutes ago", {**alert("SPY-5m-2", symbol="SPY", side="buy", qty=10),
                                       "timestamp": (now - timedelta(minutes=2)).isoformat()}),
        ("wrong shared secret", {**alert("SPY-5m-3", symbol="SPY", side="buy", qty=10), "secret": "guess"}),
        ("sell 50 AAPL, closes the trade", alert("AAPL-5m-2", symbol="AAPL", side="sell", qty=50)),
    ]


def describe(r: httpx.Response) -> str:
    body = r.json()
    if r.status_code == 401:
        return "401 unauthorized"
    status = body.get("status") or body.get("error")
    detail = body.get("reason") or "; ".join(body.get("detail", []))
    return f"{r.status_code} {status}" + (f" ({detail})" if detail else "")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m bridge.demo")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--duration", type=float, default=0, help="seconds to keep running; 0 means until Ctrl+C")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="  %(asctime)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stdout)

    db = Path(tempfile.gettempdir()) / "tv-webhook-bridge-demo.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)

    # calmer than the default feed so the demo positions live long enough to look at
    feed = FakeFeed(PRICES, seed=7, vol=0.0006, gap_chance=0.03, gap_size=0.008)
    engine, _ = build(demo_settings(str(db)), feed=feed)
    server = uvicorn.Server(uvicorn.Config(create_app(engine), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, name="uvicorn", daemon=True).start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit(f"server didn't start; is port {args.port} free?")
        time.sleep(0.05)

    stop = threading.Event()
    threading.Thread(target=feed.run, args=(1.0, stop), name="feed", daemon=True).start()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\nPaper broker, fake prices, bridge on {url}\n")
    with httpx.Client(base_url=url, timeout=5) as http:
        for label, payload in sample_alerts(feed):
            r = http.post("/webhook", json=payload)
            print(f"  {label:<36} -> {describe(r)}", flush=True)
            time.sleep(0.3)

    snap = engine.snapshot()
    held = ", ".join(f"{p['symbol']} {p['qty']:g}" for p in snap["positions"]) or "flat"
    print(f"\n  positions: {held}; working orders: {len(snap['orders'])}; "
          f"realized today: {snap['pnl']['realized']:+.2f}")
    print(f"\nDashboard: {url}  (token: {TOKEN})")
    print("Prices tick once a second and gap now and then, so stops may fill while you watch.")

    try:
        if args.duration:
            time.sleep(args.duration)
        else:
            print("Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.should_exit = True
        time.sleep(0.5)
        engine.store.close()


if __name__ == "__main__":
    main()
