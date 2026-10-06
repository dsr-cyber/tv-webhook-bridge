# Deploying

TradingView only posts webhooks to ports 80 and 443, so the bridge needs to sit
behind something that terminates TLS. Keep the bridge itself on 127.0.0.1.

Run exactly one process. The lock that stops two alerts from racing each other
through the risk checks lives in memory; a second worker would have its own.

## VPS with systemd

```sh
sudo useradd --system --home /opt/tv-webhook-bridge bridge
sudo git clone <this repo> /opt/tv-webhook-bridge
cd /opt/tv-webhook-bridge
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo cp .env.example /etc/tv-webhook-bridge.env
sudo chmod 600 /etc/tv-webhook-bridge.env
# edit it: secrets, limits, and DB_PATH=/var/lib/tv-webhook-bridge/bridge.db

sudo cp deploy/tv-webhook-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tv-webhook-bridge
journalctl -u tv-webhook-bridge -f
```

The default HOST is 127.0.0.1, which is what you want behind a proxy.

## TLS in front

Caddy is the least typing, and it flushes server-sent events without extra config:

```
bridge.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

With nginx, turn buffering off for `/events` or the dashboard will update in
bursts. The app sends `X-Accel-Buffering: no`, which nginx honours, but
`proxy_buffering off;` on that location doesn't hurt.

TradingView publishes the addresses its webhooks come from. If you want to,
allow only those on `/webhook` at the proxy, and check their support article
for the current list before you do, since it can change.

## Docker

```sh
docker build -t tv-webhook-bridge .
docker run -d --name bridge --env-file .env -p 127.0.0.1:8000:8000 \
  -v bridge-data:/data tv-webhook-bridge
```

The image sets HOST=0.0.0.0 inside the container and keeps the database on
the `/data` volume. Publish the port on 127.0.0.1 and put the proxy in front,
same as above.
