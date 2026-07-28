"""Device-generic pieces shared by routers/lan.py and routers/ble.py.

Split out once BLE (Phase 6) needed the same devices/device_sightings tables
as LAN (Phase 5) but with different staleness math: presence is judged
against each scanner's own interval, and a device row belongs to exactly one
device_type, so a stale/missing threshold and a mac_address alone are never
enough on their own, the owning device_type has to be checked too. Nothing
here reads lan_scan_settings or ble_scan_settings directly; each router
computes its own staleness threshold and passes the resulting number down.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

DeviceCategory = Literal["trusted", "iot", "guest", "unknown"]
DeviceState = Literal["present", "absent"]

VALID_CATEGORIES = {"trusted", "iot", "guest", "unknown"}


class DeviceOut(BaseModel):
    mac_address: str
    device_type: str
    vendor: str | None
    hostname: str | None
    last_ip: str | None
    last_rssi: int | None
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
    rssi: int | None
    is_present: bool


def resolve_state(last_seen: str, stale_threshold_seconds: int, now: datetime) -> DeviceState:
    try:
        ts = datetime.fromisoformat(last_seen)
    except ValueError:
        return "absent"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_seconds = (now - ts).total_seconds()
    return "present" if age_seconds <= stale_threshold_seconds else "absent"


def row_to_device(row, stale_threshold_seconds: int, now: datetime) -> DeviceOut:
    return DeviceOut(
        mac_address=row["mac_address"],
        device_type=row["device_type"],
        vendor=row["vendor"],
        hostname=row["hostname"],
        last_ip=row["last_ip"],
        last_rssi=row["last_rssi"],
        category=row["category"],
        nickname=row["nickname"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        state=resolve_state(row["last_seen"], stale_threshold_seconds, now),
    )


def update_device_row(conn, mac_address: str, device_type: str, payload: DeviceUpdate):
    """Shared PATCH body for both routers. Filters by device_type so a PATCH
    sent to /api/ble/devices/{mac} can't silently mutate a LAN-owned row
    (or vice versa) if the same mac_address ever ends up shared between a
    device's Wi-Fi and Bluetooth interfaces (see routers/ble.py's docstring
    on the accepted merge behavior for that case). Returns the updated row."""
    mac_address = mac_address.upper()
    if payload.category is not None and payload.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")

    row = conn.execute(
        "SELECT * FROM devices WHERE mac_address = ? AND device_type = ?",
        (mac_address, device_type),
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
        "UPDATE devices SET category = ?, nickname = ? WHERE mac_address = ? AND device_type = ?",
        (category, nickname, mac_address, device_type),
    )
    conn.commit()

    return conn.execute(
        "SELECT * FROM devices WHERE mac_address = ? AND device_type = ?",
        (mac_address, device_type),
    ).fetchone()


def get_sightings_rows(conn, mac_address: str, device_type: str, hours: int, limit: int):
    """Shared sightings-history query. device_sightings itself carries no
    device_type column (a sighting is just "this mac was seen at this time",
    regardless of which scanner wrote it), so ownership is checked against
    the devices row instead, same reasoning as update_device_row above."""
    if not 1 <= hours <= 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    mac_address = mac_address.upper()
    device = conn.execute(
        "SELECT mac_address FROM devices WHERE mac_address = ? AND device_type = ?",
        (mac_address, device_type),
    ).fetchone()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        """
        SELECT timestamp, ip_address, rssi, is_present FROM device_sightings
        WHERE mac_address = ? AND timestamp >= ?
        ORDER BY timestamp ASC LIMIT ?
        """,
        (mac_address, since, limit),
    ).fetchall()
