# tv-webhook-bridge

A small service that takes TradingView alert webhooks and turns them into
broker orders, with a risk layer in between and a live dashboard on the side.
It ships with a simulated paper broker and an Alpaca adapter.

I run a similar bridge for my own options trading on Indian brokers. This repo
is a clean rebuild of the same ideas for US brokers, written from scratch. It
places orders when you tell it to; it is not financial advice and I make no
claims about making money with it.

## Quick start

Python 3.11+. No keys needed for the demo.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m bridge.demo
```

The demo starts the app on the paper broker with a fake price feed and fires a
handful of alerts at it over HTTP:

```
  buy 50 AAPL with a bracket           -> 200 accepted (entry: buy 50 AAPL market -> filled (P1))
  same alert delivered twice           -> 200 duplicate (alert_id already processed)
  buy 30 QQQ, stop only                -> 200 accepted (entry: buy 30 QQQ market -> filled (P4))
  buy 20 MSFT, limit under market      -> 200 accepted (entry: buy 20 MSFT limit -> new (P6))
  buy 500 SPY, over the size limit     -> 200 refused (SPY position would be 500, max is 200)
  buy 10 TSLA, not on the allow-list   -> 200 refused (TSLA is not in the allow-list)
  alert fired 2 minutes ago            -> 200 refused (alert timestamp is 122s off (max 30s))
  wrong shared secret                  -> 401 unauthorized
  sell 50 AAPL, closes the trade       -> 200 accepted (exit: sell 50 AAPL market -> filled (P8))
```

Then open http://127.0.0.1:8000 and enter `demo-dashboard-token`. Prices keep
moving, so you can watch stops fill.

Tests: `.venv/bin/python -m pytest`.

To run it for real, copy `.env.example` to `.env`, set `WEBHOOK_SECRET` and
`DASHBOARD_TOKEN`, add Alpaca paper keys if you have them, and run
`python -m bridge`. Without Alpaca keys it runs the paper broker on synthetic
prices, which is only useful for checking your alert wiring. Deployment notes
are in [deploy/README.md](deploy/README.md).

## The alert

Paste something like this into the TradingView alert's message box and point
the webhook URL at `https://your-host/webhook`:

```json
{
  "secret": "same value as WEBHOOK_SECRET",
  "alert_id": "{{ticker}}-{{interval}}-{{time}}-{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": "{{strategy.order.contracts}}",
  "order_type": "market",
  "stop_loss": 187.50,
  "take_profit": 196.00,
  "timestamp": "{{timenow}}"
}
```

| field | required | notes |
|---|---|---|
| secret | yes | compared in constant time |
| alert_id | yes | must be unique per signal; see below |
| symbol | yes | `NASDAQ:AAPL` is cut to `AAPL` |
| side | yes | `buy` or `sell`, any case |
| qty | yes | whole shares, positive |
| order_type | no | `market` (default) or `limit` |
| limit_price | for limit | |
| stop_loss, take_profit | no | either or both; becomes a bracket or a one-legged OTO |
| timestamp | no | if present, alerts older than `MAX_ALERT_AGE_S` are refused |

`alert_id` is what makes a second delivery of the same alert harmless, so build
it from things that identify the signal, not the delivery. `{{time}}` is the
bar's open time, `{{timenow}}` is when the alert fired; the first is the one you
want in the id. For stops computed in Pine, pass them through
`alert_message` on `strategy.entry()` and use `{{strategy.order.alert_message}}`
as the whole message.

## How an alert flows

1. The body is parsed as JSON whatever the content type says. TradingView sends
   `text/plain` whenever the message doesn't parse as JSON on its side, so the
   header isn't worth much.
2. The secret is checked before anything else; a mismatch gets a 401 and isn't
   stored.
3. Pydantic validates the alert. A failure is a 422 and gets logged as a
   decision, because an unexpanded `{{placeholder}}` is the usual cause and you
   want to see it on the dashboard.
4. The `alert_id` is claimed in SQLite with an `insert or ignore`. If the row
   already existed the alert is a duplicate and nothing else happens.
5. If there's a timestamp, the age check runs.
6. Under a process-wide lock, the engine reads positions and today's realized
   P&L from the broker, counts orders sent in the last minute, and runs the
   risk rules. The lock is there because two alerts arriving together would
   otherwise both pass the position-size check.
7. An entry goes to the broker with a fresh `client_order_id` and a bracket if
   the alert had a stop or target. An exit first cancels every working order
   on that symbol, re-reads the order list until it's empty, then re-reads the
   position and sizes the exit from that.
8. Every outcome lands in the `decisions` table, the log, and Telegram if
   configured.

## Risk rules

All of them are plain functions in `bridge/risk.py`, tested without a broker or
database. The first rule that fails is the reason you see.

| rule | setting | default |
|---|---|---|
| kill switch (dashboard or `POST /kill`) | stored in SQLite, survives restarts | off |
| symbol allow-list | `ALLOWED_SYMBOLS` | empty, meaning any |
| trading window, US/Eastern | `TRADING_START`, `TRADING_END`, `TRADING_DAYS`, `TRADING_TZ` | 09:35 to 15:50, Mon-Fri |
| orders per minute | `MAX_ORDERS_PER_MINUTE` | 10 |
| daily realized loss cap | `DAILY_LOSS_CAP` | 500 |
| no naked shorts | `ALLOW_SHORT` | false |
| max shares per symbol | `MAX_POSITION_PER_SYMBOL` | 100 |
| max open positions | `MAX_OPEN_POSITIONS` | 5 |

Some choices here are judgment calls:

- The kill switch and the loss cap block new exposure, not exits. The moment
  you reach for a kill switch is the worst moment to have it refuse a sell.
- A refused alert keeps its `alert_id`. Sending the same id again later won't
  trade, even if the limit that refused it has since cleared. I'd rather lose a
  signal than act on a stale one.
- Orders go to Alpaca as `time_in_force=day`, so a limit entry from yesterday
  can't fill this morning. The price of that is that bracket legs expire at the
  close too. This is built for intraday; flatten before the bell.
- Everything happens inside the webhook request. TradingView gives up after
  three seconds, and an exit with cancel confirmation can take one to two. If
  you add slow checks, move the work to a queue and answer the webhook first.

## Things that bit me in production

These all happened on my own system, on Indian brokers. The fixes are what
shaped this code.

### A cancel that said "success" and wasn't

A working order was cancelled, the broker acknowledged it, and a replacement
went out. The original had already filled. I ended up with twice the size and
no stop on the first half. Now the cancel response is treated as a request
receipt only: exits re-read the open orders until they're actually gone, then
size from the broker's position.
`test_exit_trusts_positions_not_the_cancel_ack` covers it.

### A constant order tag

Every order went out with the same client tag. The first one worked; every
stop and exit after it was rejected. Each order now gets its own
`client_order_id`, built from the alert id plus random bytes.

### Two resting exits for one position

I once placed the stop and the target as two separate sell orders against one
long. The broker saw two sells and margined the second as a naked short, so it
was rejected. Here the bracket is the broker's own (Alpaca
`order_class=bracket`), where one leg cancels the other.

### Stops filled at prices that never traded

My backtests credited the stop price even when a bar opened well past it.
Re-run with the stop filled at the open, most of the strategies I'd thought
worked didn't. The paper broker fills a gapped stop at `min(stop, open)` for
longs and `max(stop, open)` for shorts, and when one bar touches both the stop
and the target it assumes the stop came first.

### Clock assumptions

My server ran on UTC while the code assumed exchange time, and every cutoff
was off by hours. Here everything is UTC internally and the window converts to
US/Eastern explicitly, with tests on both sides of a DST change.

One that hasn't bitten me but would: an order submit can time out after the
broker has already accepted it, and a blind retry doubles the position. On a
transport error the Alpaca adapter looks the order up by its `client_order_id`
before reporting anything.

## Limitations

- US equities, whole shares. No options, crypto or fractional shares.
- I checked the Alpaca adapter against the v2 API docs and test it with mocked
  HTTP. I have not run it against an Alpaca account. Use their paper endpoint
  (the default) first.
- Fills from Alpaca aren't recorded in the `fills` table. That needs the
  `trade_updates` stream, which isn't wired up yet. The dashboard reads
  positions and orders by polling, cached for 3 seconds to stay inside Alpaca's
  200 requests a minute.
- Alpaca has no "realized today" field. The adapter derives it from
  `equity - last_equity` minus today's move on open positions, so a deposit or
  withdrawal today will throw the loss cap off.
- A partial exit cancels the bracket and doesn't re-place a stop for what's
  left. Send full exits, or add that.
- The trading window is clock-based. It doesn't know about market holidays or
  half days, and it applies to exits too.
- Paper mode prices come from a random walk. It tells you the plumbing works,
  nothing about a strategy.
- Authentication is a shared secret in the body (TradingView can't send custom
  headers) plus a dashboard token. The dashboard's event stream takes the token
  as a query parameter because `EventSource` can't set headers; the access log
  redacts it, but use HTTPS.
- One process, SQLite. Fine for one account and a few alerts a minute.

## Layout

```
bridge/
  app.py         FastAPI routes: /webhook, /, /events, /api/state, /kill
  engine.py      alert -> risk -> broker, exit handling, dashboard snapshot
  risk.py        the rules
  models.py      the alert schema
  store.py       SQLite: alerts, decisions, orders, fills, flags
  brokers/       base types, PaperBroker, AlpacaBroker
  feed.py        fake bar feed
  demo.py        python -m bridge.demo
  static/        dashboard (plain HTML, CSS, JS)
tests/
deploy/          systemd unit and notes
```

MIT licensed.
