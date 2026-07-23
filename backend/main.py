"""Lares API: reads what the collectors have written, plus live Docker
actions (stop/restart/logs) which need to hit the Docker Engine API
directly rather than the database. No collector is a hard dependency of
this starting; init_db() is idempotent so the schema exists even if this
runs before any collector ever has.

Run with: uvicorn backend.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

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
