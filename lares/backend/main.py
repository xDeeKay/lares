"""Lares API: reads what the collectors have written, plus live Docker
actions (stop/restart/logs) which need to hit the Docker Engine API
directly rather than the database. No collector is a hard dependency of
this starting; init_db() is idempotent so the schema exists even if this
runs before any collector ever has.

Run with: uvicorn backend.main:app --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from backend.auth import require_auth
from backend.db import init_db
from backend.routers import auth, containers, disk, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Lares", version="0.1.0", lifespan=lifespan)

app.include_router(system.router, dependencies=[Depends(require_auth)])
app.include_router(disk.router, dependencies=[Depends(require_auth)])
app.include_router(containers.router, dependencies=[Depends(require_auth)])
app.include_router(auth.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# Serves the built frontend when it's present (the Docker image builds it
# in). Mounted last so it can never shadow /api/* or /health, and skipped
# entirely when frontend/dist doesn't exist (e.g. the Pi's bare-process dev
# setup, which has no Node/frontend build at all) rather than crashing the
# whole API on startup, matching the collectors' "not a hard dependency"
# pattern.
_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
