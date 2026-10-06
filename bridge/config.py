import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env reader. Real environment variables always win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _days(value: str) -> frozenset[int]:
    picked = {DAYS.index(d.strip().lower()[:3]) for d in value.split(",") if d.strip()}
    if not picked:
        raise ValueError("TRADING_DAYS is empty")
    return frozenset(picked)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    dashboard_token: str
    db_path: str = "bridge.db"
    broker: str = "paper"

    max_alert_age_s: int = 30
    max_position_per_symbol: int = 100
    max_open_positions: int = 5
    daily_loss_cap: float = 500.0
    max_orders_per_minute: int = 10
    allow_short: bool = False
    trading_start: time = time(9, 35)
    trading_end: time = time(15, 50)
    trading_days: frozenset[int] = frozenset(range(5))  # Monday=0
    timezone: str = "America/New_York"
    allowed_symbols: frozenset[str] = field(default_factory=frozenset)

    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        secret = env.get("WEBHOOK_SECRET", "")
        token = env.get("DASHBOARD_TOKEN", "")
        if len(secret) < 16:
            raise ValueError("WEBHOOK_SECRET must be set and at least 16 characters")
        if len(token) < 16:
            raise ValueError("DASHBOARD_TOKEN must be set and at least 16 characters")

        key_id = env.get("ALPACA_KEY_ID", "")
        secret_key = env.get("ALPACA_SECRET_KEY", "")
        broker = env.get("BROKER") or ("alpaca" if key_id and secret_key else "paper")
        if broker not in ("paper", "alpaca"):
            raise ValueError(f"BROKER must be paper or alpaca, got {broker!r}")
        if broker == "alpaca" and not (key_id and secret_key):
            raise ValueError("BROKER=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET_KEY")

        symbols = env.get("ALLOWED_SYMBOLS", "")
        return cls(
            webhook_secret=secret,
            dashboard_token=token,
            db_path=env.get("DB_PATH", "bridge.db"),
            broker=broker,
            max_alert_age_s=int(env.get("MAX_ALERT_AGE_S", 30)),
            max_position_per_symbol=int(env.get("MAX_POSITION_PER_SYMBOL", 100)),
            max_open_positions=int(env.get("MAX_OPEN_POSITIONS", 5)),
            daily_loss_cap=float(env.get("DAILY_LOSS_CAP", 500)),
            max_orders_per_minute=int(env.get("MAX_ORDERS_PER_MINUTE", 10)),
            allow_short=_bool(env.get("ALLOW_SHORT")),
            trading_start=_hhmm(env.get("TRADING_START", "09:35")),
            trading_end=_hhmm(env.get("TRADING_END", "15:50")),
            trading_days=_days(env.get("TRADING_DAYS", "mon,tue,wed,thu,fri")),
            timezone=env.get("TRADING_TZ", "America/New_York"),
            allowed_symbols=frozenset(s.strip().upper() for s in symbols.split(",") if s.strip()),
            alpaca_key_id=key_id,
            alpaca_secret_key=secret_key,
            alpaca_base_url=env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        )
