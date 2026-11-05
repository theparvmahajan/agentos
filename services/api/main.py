import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

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


app = FastAPI(title="AgentOS", version="0.2.0", lifespan=lifespan)
app.include_router(missions_router)
app.include_router(stream_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
