from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection
from backend.routers.device_shared import (
    DeviceOut,
    DeviceSightingOut,
    DeviceUpdate,
    get_sightings_rows,
    row_to_device,
    update_device_row,
)

router = APIRouter(prefix="/api/lan", tags=["lan"])

DEFAULT_SCAN_INTERVAL_SECONDS = 300
# A device is considered "absent" once its last sighting is older than this
# many scan cycles, mirroring how routers/uptime.py derives "stale" from
# check-history age rather than storing a separate presence flag.
STALE_CYCLE_MULTIPLIER = 3


class LanSettingsOut(BaseModel):
    cidr: str | None
    effective_cidr: str | None
    scan_interval_seconds: int
    last_scan_at: str | None


class LanSettingsUpdate(BaseModel):
    cidr: str | None = None
    scan_interval_seconds: int | None = None
    # Same explicit-clear pattern as DeviceUpdate.clear_nickname: distinguishes
    # "go back to auto-detect" from "field not provided".
    clear_cidr: bool = False


def _row_to_settings(row) -> LanSettingsOut:
    return LanSettingsOut(
        cidr=row["cidr"],
        effective_cidr=row["effective_cidr"],
        scan_interval_seconds=row["scan_interval_seconds"],
        last_scan_at=row["last_scan_at"],
    )


@router.get("/devices", response_model=list[DeviceOut])
def list_devices():
    conn = get_connection()
    try:
        settings = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
        scan_interval_seconds = (
            settings["scan_interval_seconds"] if settings else DEFAULT_SCAN_INTERVAL_SECONDS
        )
        # Filtered to device_type = 'lan': without this, a BLE row (Phase 6)
        # would show up in the LAN devices list too, and have its presence
        # judged against LAN's scan interval instead of BLE's.
        rows = conn.execute(
            "SELECT * FROM devices WHERE device_type = 'lan' ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    stale_threshold = scan_interval_seconds * STALE_CYCLE_MULTIPLIER
    return [row_to_device(row, stale_threshold, now) for row in rows]


@router.patch("/devices/{mac_address}", response_model=DeviceOut)
def update_device(mac_address: str, payload: DeviceUpdate):
    conn = get_connection()
    try:
        updated = update_device_row(conn, mac_address, "lan", payload)
        settings = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
        scan_interval_seconds = (
            settings["scan_interval_seconds"] if settings else DEFAULT_SCAN_INTERVAL_SECONDS
        )
    finally:
        conn.close()
    stale_threshold = scan_interval_seconds * STALE_CYCLE_MULTIPLIER
    return row_to_device(updated, stale_threshold, datetime.now(timezone.utc))


@router.get("/devices/{mac_address}/sightings", response_model=list[DeviceSightingOut])
def get_device_sightings(mac_address: str, hours: int = 24, limit: int = 2000):
    conn = get_connection()
    try:
        rows = get_sightings_rows(conn, mac_address, "lan", hours, limit)
    finally:
        conn.close()
    return [
        DeviceSightingOut(
            timestamp=r["timestamp"],
            ip_address=r["ip_address"],
            rssi=r["rssi"],
            is_present=bool(r["is_present"]),
        )
        for r in rows
    ]


@router.get("/settings", response_model=LanSettingsOut)
def get_settings():
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=500, detail="LAN scan settings row is missing")
    return _row_to_settings(row)


@router.patch("/settings", response_model=LanSettingsOut)
def update_settings(payload: LanSettingsUpdate):
    if payload.scan_interval_seconds is not None and payload.scan_interval_seconds < 30:
        raise HTTPException(status_code=400, detail="scan_interval_seconds must be at least 30")

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="LAN scan settings row is missing")

        if payload.clear_cidr:
            cidr = None
        elif payload.cidr is not None:
            cidr = payload.cidr
        else:
            cidr = row["cidr"]

        scan_interval_seconds = (
            payload.scan_interval_seconds
            if payload.scan_interval_seconds is not None
            else row["scan_interval_seconds"]
        )

        conn.execute(
            """
            UPDATE lan_scan_settings
            SET cidr = ?, scan_interval_seconds = ?, updated_at = ?
            WHERE id = 1
            """,
            (cidr, scan_interval_seconds, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    return _row_to_settings(updated)


@router.post("/scan-now", response_model=LanSettingsOut)
def scan_now():
    """Nudges the LAN scanner collector (running in a separate,
    host-networked container, see docker-compose.yml) to scan on its next
    tick rather than waiting out the configured interval. There's no
    in-process equivalent to routers/uptime.py's check_target_now, since
    that collector shares a container with this API and this one doesn't:
    a DB flag is the only cross-container nudge available."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="LAN scan settings row is missing")
        conn.execute(
            "UPDATE lan_scan_settings SET force_scan_requested_at = ? WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    return _row_to_settings(updated)
