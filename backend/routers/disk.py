import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection

router = APIRouter(prefix="/api", tags=["disk"])

# All three resolved once at process startup (env vars, not live config) and
# surfaced verbatim via /api/config/disk so the frontend never re-derives or
# hardcodes a copy.
#
# LARES_DISK_STALE_SECONDS / LARES_DISK_MISSING_SECONDS default to 2x/6x of
# LARES_DISK_POLL_INTERVAL, but that coupling only holds while they're unset.
# Setting either explicitly makes it a fixed value that stops tracking the
# poll interval, i.e. changing LARES_DISK_POLL_INTERVAL later won't move it.
# Restarting the API is required either way for a changed interval to take
# effect, since these are read once at import time, not polled.
DISK_POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_DISK_POLL_INTERVAL", 300))
# stale: a poll or two has been missed, but could just be a transient hiccup.
DISK_STALE_THRESHOLD_SECONDS = float(
    os.environ.get("LARES_DISK_STALE_SECONDS", DISK_POLL_INTERVAL_SECONDS * 2)
)
# missing: long enough that this is very likely a removed drive, not a slow
# poll cycle. The API only classifies it; the dashboard decides what to do
# with that tier (gray out, hide, etc).
DISK_MISSING_THRESHOLD_SECONDS = float(
    os.environ.get("LARES_DISK_MISSING_SECONDS", DISK_POLL_INTERVAL_SECONDS * 6)
)

DiskFreshness = Literal["fresh", "stale", "missing"]


class DiskInfoOut(BaseModel):
    device: str
    mount_point: str
    timestamp: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_pct: float
    # Computed at query time against the current clock, not stored: a row's
    # tier can advance from fresh -> stale -> missing between polls even
    # though the row itself never changes.
    freshness: DiskFreshness


class DiskConfigOut(BaseModel):
    poll_interval_seconds: float
    stale_threshold_seconds: float
    missing_threshold_seconds: float


def classify_disk_freshness(timestamp_str: str) -> DiskFreshness:
    """Tier a disk_info row's age against the module's stale/missing thresholds.

    Kept standalone (not folded into _row_to_disk_info) so the same rule can
    back a future "drive went missing" alert without duplicating the logic.
    """
    try:
        ts = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return "missing"  # unparseable timestamp: fail to the most conservative tier
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if age_seconds > DISK_MISSING_THRESHOLD_SECONDS:
        return "missing"
    if age_seconds > DISK_STALE_THRESHOLD_SECONDS:
        return "stale"
    return "fresh"


def _row_to_disk_info(row) -> DiskInfoOut:
    return DiskInfoOut(
        device=row["device"],
        mount_point=row["mount_point"],
        timestamp=row["timestamp"],
        total_gb=row["total_gb"],
        used_gb=row["used_gb"],
        free_gb=row["free_gb"],
        used_pct=row["used_pct"],
        freshness=classify_disk_freshness(row["timestamp"]),
    )


@router.get("/config/disk", response_model=DiskConfigOut)
def get_disk_config():
    return DiskConfigOut(
        poll_interval_seconds=DISK_POLL_INTERVAL_SECONDS,
        stale_threshold_seconds=DISK_STALE_THRESHOLD_SECONDS,
        missing_threshold_seconds=DISK_MISSING_THRESHOLD_SECONDS,
    )


@router.get("/disk/latest", response_model=list[DiskInfoOut])
def get_latest_disk_info():
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT d.* FROM disk_info d
            INNER JOIN (
                SELECT mount_point, MAX(timestamp) AS max_ts
                FROM disk_info
                GROUP BY mount_point
            ) latest
                ON d.mount_point = latest.mount_point
                AND d.timestamp = latest.max_ts
            ORDER BY d.mount_point
            """
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_disk_info(row) for row in rows]


@router.get("/disk/history", response_model=list[DiskInfoOut])
def get_disk_history(minutes: int = 1440, mount_point: str | None = None, limit: int = 2000):
    if not 1 <= minutes <= 43200:  # cap the window at 30 days
        raise HTTPException(status_code=400, detail="minutes must be between 1 and 43200")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    query = "SELECT * FROM disk_info WHERE timestamp >= ?"
    params: list = [since]
    if mount_point is not None:
        query += " AND mount_point = ?"
        params.append(mount_point)
    query += " ORDER BY timestamp ASC LIMIT ?"
    params.append(limit)

    conn = get_connection()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [_row_to_disk_info(row) for row in rows]
