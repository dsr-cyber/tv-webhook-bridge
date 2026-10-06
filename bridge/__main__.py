import logging
import os
import threading

import uvicorn

from .app import create_app
from .config import Settings, load_dotenv
from .server import RedactTokens, build


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(RedactTokens())
    load_dotenv()
    settings = Settings.from_env()
    engine, feed = build(settings)

    stop = threading.Event()
    if feed is not None:
        logging.getLogger("bridge").warning("paper broker on a synthetic price feed; nothing here is real")
        threading.Thread(target=feed.run, args=(1.0, stop), name="feed", daemon=True).start()

    try:
        uvicorn.run(
            create_app(engine),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            log_config=None,  # keep the logging set up above, including the redaction filter
        )
    finally:
        stop.set()
        engine.notifier.close()
        engine.store.close()


if __name__ == "__main__":
    main()
