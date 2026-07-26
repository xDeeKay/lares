from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection

router = APIRouter(prefix="/api/lan", tags=["lan"])

DeviceCategory = Literal["trusted", "iot", "guest", "unknown"]
DeviceState = Literal["present", "absent"]

VALID_CATEGORIES = {"trusted", "iot", "guest", "unknown"}
DEFAULT_SCAN_INTERVAL_SECONDS = 300
# A device is considered "absent" once its last sighting is older than this
# many scan cycles, mirroring how routers/uptime.py derives "stale" from
# check-history age rather than storing a separate presence flag.
STALE_CYCLE_MULTIPLIER = 3


class DeviceOut(BaseModel):
    mac_address: str
    device_type: str
    vendor: str | None
    hostname: str | None
    last_ip: str | None
    category: DeviceCategory
    nickname: str | None
    first_seen: str
    last_seen: str
    state: DeviceState


class DeviceUpdate(BaseModel):
    category: DeviceCategory | None = None
    nickname: str | None = None
    # Explicit flag so a PATCH can clear a nickname back to null, which a
    # plain "field not provided" can't distinguish from "leave it as-is".
    clear_nickname: bool = False


class DeviceSightingOut(BaseModel):
    timestamp: str
    ip_address: str | None
    is_present: bool


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


def _resolve_state(last_seen: str, scan_interval_seconds: int, now: datetime) -> DeviceState:
    try:
        ts = datetime.fromisoformat(last_seen)
    except ValueError:
        return "absent"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    threshold = scan_interval_seconds * STALE_CYCLE_MULTIPLIER
    age_seconds = (now - ts).total_seconds()
    return "present" if age_seconds <= threshold else "absent"


def _row_to_device(row, scan_interval_seconds: int, now: datetime) -> DeviceOut:
    return DeviceOut(
        mac_address=row["mac_address"],
        device_type=row["device_type"],
        vendor=row["vendor"],
        hostname=row["hostname"],
        last_ip=row["last_ip"],
        category=row["category"],
        nickname=row["nickname"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        state=_resolve_state(row["last_seen"], scan_interval_seconds, now),
    )


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
        rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    return [_row_to_device(row, scan_interval_seconds, now) for row in rows]


@router.patch("/devices/{mac_address}", response_model=DeviceOut)
def update_device(mac_address: str, payload: DeviceUpdate):
    mac_address = mac_address.upper()
    if payload.category is not None and payload.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM devices WHERE mac_address = ?", (mac_address,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")

        category = payload.category if payload.category is not None else row["category"]
        if payload.clear_nickname:
            nickname = None
        elif payload.nickname is not None:
            nickname = payload.nickname
        else:
            nickname = row["nickname"]

        conn.execute(
            "UPDATE devices SET category = ?, nickname = ? WHERE mac_address = ?",
            (category, nickname, mac_address),
        )
        conn.commit()

        settings = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
        scan_interval_seconds = (
            settings["scan_interval_seconds"] if settings else DEFAULT_SCAN_INTERVAL_SECONDS
        )
        updated = conn.execute(
            "SELECT * FROM devices WHERE mac_address = ?", (mac_address,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_device(updated, scan_interval_seconds, datetime.now(timezone.utc))


@router.get("/devices/{mac_address}/sightings", response_model=list[DeviceSightingOut])
def get_device_sightings(mac_address: str, hours: int = 24, limit: int = 2000):
    if not 1 <= hours <= 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    mac_address = mac_address.upper()
    conn = get_connection()
    try:
        device = conn.execute(
            "SELECT mac_address FROM devices WHERE mac_address = ?", (mac_address,)
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """
            SELECT timestamp, ip_address, is_present FROM device_sightings
            WHERE mac_address = ? AND timestamp >= ?
            ORDER BY timestamp ASC LIMIT ?
            """,
            (mac_address, since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        DeviceSightingOut(
            timestamp=r["timestamp"], ip_address=r["ip_address"], is_present=bool(r["is_present"])
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
