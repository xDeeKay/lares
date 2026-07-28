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

router = APIRouter(prefix="/api/ble", tags=["ble"])

DEFAULT_FLUSH_INTERVAL_SECONDS = 30
# Smaller than lan.py's STALE_CYCLE_MULTIPLIER (3), and applied to a much
# shorter base interval (30s flush vs LAN's 300s scan): BLE presence should
# reflect "did we hear this MAC recently," not "is this a stable long-term
# identity", since most phones rotate their BLE MAC address for privacy on
# the order of every ~15 minutes when not paired. A generous multiplier here
# would just make an already-rotated-away device look "present" long after
# it's actually gone.
STALE_CYCLE_MULTIPLIER = 2


class BleSettingsOut(BaseModel):
    flush_interval_seconds: int
    last_flush_at: str | None


class BleSettingsUpdate(BaseModel):
    flush_interval_seconds: int | None = None


def _row_to_settings(row) -> BleSettingsOut:
    return BleSettingsOut(
        flush_interval_seconds=row["flush_interval_seconds"],
        last_flush_at=row["last_flush_at"],
    )


@router.get("/devices", response_model=list[DeviceOut])
def list_devices():
    conn = get_connection()
    try:
        settings = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
        flush_interval_seconds = (
            settings["flush_interval_seconds"] if settings else DEFAULT_FLUSH_INTERVAL_SECONDS
        )
        rows = conn.execute(
            "SELECT * FROM devices WHERE device_type = 'ble' ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    stale_threshold = flush_interval_seconds * STALE_CYCLE_MULTIPLIER
    return [row_to_device(row, stale_threshold, now) for row in rows]


@router.patch("/devices/{mac_address}", response_model=DeviceOut)
def update_device(mac_address: str, payload: DeviceUpdate):
    conn = get_connection()
    try:
        updated = update_device_row(conn, mac_address, "ble", payload)
        settings = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
        flush_interval_seconds = (
            settings["flush_interval_seconds"] if settings else DEFAULT_FLUSH_INTERVAL_SECONDS
        )
    finally:
        conn.close()
    stale_threshold = flush_interval_seconds * STALE_CYCLE_MULTIPLIER
    return row_to_device(updated, stale_threshold, datetime.now(timezone.utc))


@router.get("/devices/{mac_address}/sightings", response_model=list[DeviceSightingOut])
def get_device_sightings(mac_address: str, hours: int = 24, limit: int = 2000):
    conn = get_connection()
    try:
        rows = get_sightings_rows(conn, mac_address, "ble", hours, limit)
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


@router.get("/settings", response_model=BleSettingsOut)
def get_settings():
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=500, detail="BLE scan settings row is missing")
    return _row_to_settings(row)


@router.patch("/settings", response_model=BleSettingsOut)
def update_settings(payload: BleSettingsUpdate):
    # Lower floor than LAN's 30s: BLE flushing is just an in-memory buffer
    # write, not a network scan, so more frequent flushing is cheap. Still
    # bounded above 0 to keep the "batch writes, don't write every
    # advertisement" principle intact (see collectors/ble.py).
    if payload.flush_interval_seconds is not None and payload.flush_interval_seconds < 15:
        raise HTTPException(status_code=400, detail="flush_interval_seconds must be at least 15")

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="BLE scan settings row is missing")

        flush_interval_seconds = (
            payload.flush_interval_seconds
            if payload.flush_interval_seconds is not None
            else row["flush_interval_seconds"]
        )

        conn.execute(
            """
            UPDATE ble_scan_settings
            SET flush_interval_seconds = ?, updated_at = ?
            WHERE id = 1
            """,
            (flush_interval_seconds, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    return _row_to_settings(updated)


@router.post("/flush-now", response_model=BleSettingsOut)
def flush_now():
    """Nudges the BLE scanner collector (running in a separate container,
    see docker-compose.yml) to flush its in-memory advertisement buffer to
    disk immediately rather than waiting out flush_interval_seconds. Same
    cross-container DB-flag nudge as routers/lan.py's scan_now, for the same
    reason: this API and the collector don't share a process."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="BLE scan settings row is missing")
        conn.execute(
            "UPDATE ble_scan_settings SET force_flush_requested_at = ? WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
    finally:
        conn.close()
    return _row_to_settings(updated)
