import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
create table if not exists alerts (
    alert_id    text primary key,
    received_at text not null,
    payload     text not null,
    outcome     text
);

create table if not exists decisions (
    id       integer primary key autoincrement,
    ts       text not null,
    alert_id text,
    symbol   text,
    side     text,
    qty      integer,
    outcome  text not null,
    rule     text,
    reason   text
);

create table if not exists orders (
    id              integer primary key autoincrement,
    ts              text not null,
    alert_id        text,
    client_order_id text not null unique,
    broker_order_id text,
    symbol          text not null,
    side            text not null,
    qty             real not null,
    order_type      text not null,
    limit_price     real,
    stop_loss       real,
    take_profit     real,
    status          text
);
create index if not exists orders_ts on orders (ts);

create table if not exists fills (
    id              integer primary key autoincrement,
    ts              text not null,
    broker_order_id text,
    client_order_id text,
    symbol          text not null,
    side            text not null,
    qty             real not null,
    price           real not null
);

create table if not exists flags (
    key   text primary key,
    value text not null
);
"""


def _ts(dt: datetime) -> str:
    # One fixed format so string comparison in SQL is also time comparison.
    return dt.isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("pragma journal_mode=wal")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params=()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def _fetch(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def claim_alert(self, alert_id: str, payload: dict, now: datetime) -> bool:
        """Record an alert id. False means we've seen it before."""
        inserted = self._exec(
            "insert or ignore into alerts (alert_id, received_at, payload) values (?, ?, ?)",
            (alert_id, _ts(now), json.dumps(payload)),
        )
        return inserted == 1

    def set_alert_outcome(self, alert_id: str, outcome: str) -> None:
        self._exec("update alerts set outcome = ? where alert_id = ?", (outcome, alert_id))

    def add_decision(self, now: datetime, outcome: str, *, alert_id=None, symbol=None,
                     side=None, qty=None, rule="", reason="") -> None:
        self._exec(
            "insert into decisions (ts, alert_id, symbol, side, qty, outcome, rule, reason)"
            " values (?, ?, ?, ?, ?, ?, ?, ?)",
            (_ts(now), alert_id, symbol, side, qty, outcome, rule, reason),
        )

    def recent_decisions(self, limit: int = 20) -> list[dict]:
        rows = self._fetch(
            "select ts, alert_id, symbol, side, qty, outcome, rule, reason"
            " from decisions order by id desc limit ?", (limit,),
        )
        return [dict(r) for r in rows]

    def add_order(self, now: datetime, alert_id: str, req, order) -> None:
        self._exec(
            "insert into orders (ts, alert_id, client_order_id, broker_order_id, symbol, side,"
            " qty, order_type, limit_price, stop_loss, take_profit, status)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_ts(now), alert_id, req.client_order_id, order.id, req.symbol, req.side, req.qty,
             req.order_type, req.limit_price, req.stop_loss, req.take_profit, order.status),
        )

    def orders_since(self, since: datetime) -> int:
        return self._fetch("select count(*) from orders where ts >= ?", (_ts(since),))[0][0]

    def add_fill(self, fill) -> None:
        self._exec(
            "insert into fills (ts, broker_order_id, client_order_id, symbol, side, qty, price)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (_ts(fill.ts), fill.order_id, fill.client_order_id, fill.symbol, fill.side,
             fill.qty, fill.price),
        )
        self._exec("update orders set status = 'filled' where client_order_id = ?",
                   (fill.client_order_id,))

    def get_flag(self, key: str, default: str = "") -> str:
        rows = self._fetch("select value from flags where key = ?", (key,))
        return rows[0][0] if rows else default

    def set_flag(self, key: str, value: str) -> None:
        self._exec(
            "insert into flags (key, value) values (?, ?)"
            " on conflict (key) do update set value = excluded.value",
            (key, value),
        )
