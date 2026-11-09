import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from core.logging_setup import configure_logging
from services.api.missions import router as missions_router
from services.api.stream import EventHub, make_listener
from services.api.stream import router as stream_router

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the database listener that feeds every WebSocket; stop it on shutdown."""
    hub = EventHub()
    listener = make_listener(hub, asyncio.get_running_loop())
    app.state.hub = hub
    app.state.listener = listener
    listener.start()
    try:
        yield
    finally:
        app.state.hub = None  # no feed any more: new sockets are told so
        listener.stop()
        await asyncio.to_thread(listener.join, 3)


app = FastAPI(title="AgentOS", version="0.3.0", lifespan=lifespan)
# Lets the dashboard (a different origin) call the API from the browser. Only the listed origins,
# only the methods and header the dashboard uses, and no cookies. WebSockets are not covered by
# CORS; the feed is read-only and unauthenticated until milestone M5.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
    expose_headers=["Location", "Idempotent-Replay"],
)
app.include_router(missions_router)
app.include_router(stream_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
