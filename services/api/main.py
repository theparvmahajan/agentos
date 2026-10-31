from fastapi import FastAPI

from core.logging_setup import configure_logging
from services.api.missions import router as missions_router

configure_logging()

app = FastAPI(title="AgentOS", version="0.1.0")
app.include_router(missions_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}