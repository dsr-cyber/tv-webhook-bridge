import hmac
import json
import logging

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .engine import Engine
from .models import Alert

log = logging.getLogger("bridge")


def _same(given, expected: str) -> bool:
    if not isinstance(given, str) or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="tv-webhook-bridge", docs_url=None, redoc_url=None)
    app.state.engine = engine
    settings = engine.settings

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "broker": engine.broker.name}

    @app.post("/webhook")
    async def webhook(request: Request):
        # TradingView sends text/plain whenever the message isn't valid JSON, and
        # application/json when it is. Ignore the header and just try to parse.
        raw = await request.body()
        try:
            data = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        if not _same(data.get("secret"), settings.webhook_secret):
            host = request.client.host if request.client else "?"
            log.warning("webhook with bad secret from %s", host)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            alert = Alert.model_validate(data)
        except ValidationError as e:
            problems = [f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()]
            engine.record_invalid(data.get("alert_id"), "; ".join(problems)[:300])
            return JSONResponse({"error": "invalid alert", "detail": problems}, status_code=422)

        result = await run_in_threadpool(engine.handle, alert)
        return JSONResponse(result.to_dict(), status_code=result.http_status)

    return app
