import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

log = logging.getLogger(__name__)


class Notifier:
    """Fire-and-forget Telegram messages. Does nothing unless both settings are present."""

    def __init__(self, bot_token: str = "", chat_id: str = "", client: httpx.Client | None = None):
        self.enabled = bool(bot_token and chat_id)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client
        # One worker so messages arrive in order and a slow Telegram never
        # holds up the webhook response.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify") if self.enabled else None

    def send(self, text: str) -> None:
        if self._pool is not None:
            self._pool.submit(self._post, text)

    def _post(self, text: str) -> None:
        try:
            client = self._client or httpx
            r = client.post(self._url, json={"chat_id": self._chat_id, "text": text}, timeout=5.0)
            if r.status_code != 200:
                log.warning("telegram returned %s", r.status_code)
        except httpx.HTTPError as e:
            # Not str(e): httpx puts the URL in the message, and the bot token is in the URL.
            log.warning("telegram send failed: %s", type(e).__name__)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
