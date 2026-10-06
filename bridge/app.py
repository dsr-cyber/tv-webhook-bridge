import asyncio
import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .brokers.base import BrokerError
from .engine import Engine
from .models import Alert

log = logging.getLogger("bridge")
STATIC = Path(__file__).parent / "static"
UNAUTHORIZED = {"error": "unauthorized"}


def _same(given, expected: str) -> bool:
    if not isinstance(given, str) or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


async def sse_events(get_snapshot, is_disconnected, interval: float = 1.0, keepalive_every: int = 15):
    """Server-sent events: a frame whenever the state changes, a comment when it hasn't for a while."""
    last, quiet = None, 0
    while not await is_disconnected():
        body = json.dumps(await get_snapshot(), default=str)
        if body != last:
            yield f"data: {body}\n\n"
            last, quiet = body, 0
        else:
            quiet += 1
            if quiet >= keepalive_every:  # keeps proxies from closing an idle stream
                yield ": keepalive\n\n"
                quiet = 0
        await asyncio.sleep(interval)


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
            return JSONResponse(UNAUTHORIZED, status_code=401)

        try:
            alert = Alert.model_validate(data)
        except ValidationError as e:
            problems = [f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()]
            engine.record_invalid(data.get("alert_id"), "; ".join(problems)[:300])
            return JSONResponse({"error": "invalid alert", "detail": problems}, status_code=422)

        result = await run_in_threadpool(engine.handle, alert)
        return JSONResponse(result.to_dict(), status_code=result.http_status)

    def dashboard_ok(request: Request) -> bool:
        # EventSource can't set headers, so /events takes the token as a query
        # parameter. Everything else should use the header.
        token = request.headers.get("x-dashboard-token") or request.query_params.get("token")
        return _same(token, settings.dashboard_token)

    async def snapshot() -> dict:
        try:
            return await run_in_threadpool(engine.snapshot)
        except BrokerError as e:
            return {"error": str(e)}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state(request: Request):
        if not dashboard_ok(request):
            return JSONResponse(UNAUTHORIZED, status_code=401)
        return await snapshot()

    @app.post("/kill")
    async def kill(request: Request):
        if not dashboard_ok(request):
            return JSONResponse(UNAUTHORIZED, status_code=401)
        try:
            on = (await request.json()).get("on")
        except (ValueError, AttributeError):
            on = None
        if not isinstance(on, bool):
            return JSONResponse({"error": 'send {"on": true} or {"on": false}'}, status_code=400)
        await run_in_threadpool(engine.set_kill_switch, on, "dashboard")
        return {"kill_switch": on}

    @app.get("/events")
    async def events(request: Request):
        if not dashboard_ok(request):
            return JSONResponse(UNAUTHORIZED, status_code=401)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        stream = sse_events(snapshot, request.is_disconnected)
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
