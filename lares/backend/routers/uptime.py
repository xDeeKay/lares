import os
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection

router = APIRouter(prefix="/api/uptime", tags=["uptime"])

# stale: checks have stopped arriving (collector down, or a target was just
# added and hasn't been polled yet vs. one that used to report and went
# quiet) — a distinct state from a genuine "down" reading, not collapsed
# into it.
UPTIME_POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_UPTIME_POLL_INTERVAL", 30))
UPTIME_STALE_THRESHOLD_SECONDS = float(
    os.environ.get("LARES_UPTIME_STALE_SECONDS", UPTIME_POLL_INTERVAL_SECONDS * 3)
)

TargetType = Literal["http", "tcp", "ping"]
UptimeState = Literal["up", "down", "stale", "unknown"]


class UptimeTargetOut(BaseModel):
    id: int
    name: str
    target_type: TargetType
    address: str
    enabled: bool
    created_at: str


class UptimeTargetCreate(BaseModel):
    name: str
    target_type: TargetType
    address: str
    enabled: bool = True


class UptimeTargetUpdate(BaseModel):
    name: str | None = None
    target_type: TargetType | None = None
    address: str | None = None
    enabled: bool | None = None


class UptimeStatusOut(BaseModel):
    target: UptimeTargetOut
    state: UptimeState
    last_checked: str | None
    response_ms: int | None
    sla_24h_pct: float | None
    sla_7d_pct: float | None


def _validate_address(target_type: str, address: str) -> None:
    if not address or not address.strip():
        raise HTTPException(status_code=400, detail="address must not be empty")

    if target_type == "http":
        parsed = urlparse(address)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=400,
                detail="http targets need a full URL starting with http:// or https://",
            )
    elif target_type == "tcp":
        host, sep, port_str = address.rpartition(":")
        if not sep or not host:
            raise HTTPException(status_code=400, detail='tcp targets must be "host:port"')
        try:
            port = int(port_str)
        except ValueError:
            port = -1
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=400, detail="tcp port must be between 1 and 65535")
    # ping: any non-empty string is accepted (hostname or IP)


def _row_to_target(row) -> UptimeTargetOut:
    return UptimeTargetOut(
        id=row["id"],
        name=row["name"],
        target_type=row["target_type"],
        address=row["address"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def _compute_sla(conn, target_id: int, since_iso: str) -> float | None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_up THEN 1 ELSE 0 END) AS up_count
        FROM uptime_checks
        WHERE target_id = ? AND timestamp >= ?
        """,
        (target_id, since_iso),
    ).fetchone()
    if not row or not row["total"]:
        return None
    return round(row["up_count"] * 100.0 / row["total"], 1)


def _resolve_state(latest, now: datetime) -> tuple[UptimeState, str | None, int | None]:
    if latest is None:
        return "unknown", None, None

    timestamp_str = latest["timestamp"]
    response_ms = latest["response_ms"]
    try:
        ts = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return "stale", timestamp_str, response_ms
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    age_seconds = (now - ts).total_seconds()
    if age_seconds > UPTIME_STALE_THRESHOLD_SECONDS:
        return "stale", timestamp_str, response_ms

    return ("up" if latest["is_up"] else "down"), timestamp_str, response_ms


@router.get("/targets", response_model=list[UptimeTargetOut])
def list_targets():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM uptime_targets ORDER BY name").fetchall()
    finally:
        conn.close()
    return [_row_to_target(row) for row in rows]


@router.post("/targets", response_model=UptimeTargetOut, status_code=201)
def create_target(payload: UptimeTargetCreate):
    _validate_address(payload.target_type, payload.address)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO uptime_targets (name, target_type, address, enabled, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.target_type,
                payload.address,
                payload.enabled,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM uptime_targets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_target(row)


@router.patch("/targets/{target_id}", response_model=UptimeTargetOut)
def update_target(target_id: int, payload: UptimeTargetUpdate):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Target not found")

        name = payload.name if payload.name is not None else row["name"]
        target_type = payload.target_type if payload.target_type is not None else row["target_type"]
        address = payload.address if payload.address is not None else row["address"]
        enabled = payload.enabled if payload.enabled is not None else bool(row["enabled"])

        if payload.address is not None or payload.target_type is not None:
            _validate_address(target_type, address)

        conn.execute(
            """
            UPDATE uptime_targets
            SET name = ?, target_type = ?, address = ?, enabled = ?
            WHERE id = ?
            """,
            (name, target_type, address, enabled, target_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_target(updated)


@router.delete("/targets/{target_id}", status_code=204)
def delete_target(target_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Target not found")
        conn.execute("DELETE FROM uptime_checks WHERE target_id = ?", (target_id,))
        conn.execute("DELETE FROM uptime_targets WHERE id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()


@router.get("/status", response_model=list[UptimeStatusOut])
def get_uptime_status():
    conn = get_connection()
    try:
        targets = conn.execute("SELECT * FROM uptime_targets ORDER BY name").fetchall()
        now = datetime.now(timezone.utc)
        since_24h = (now - timedelta(hours=24)).isoformat()
        since_7d = (now - timedelta(days=7)).isoformat()

        results = []
        for t in targets:
            latest = conn.execute(
                "SELECT * FROM uptime_checks WHERE target_id = ? ORDER BY timestamp DESC LIMIT 1",
                (t["id"],),
            ).fetchone()
            state, last_checked, response_ms = _resolve_state(latest, now)
            results.append(
                UptimeStatusOut(
                    target=_row_to_target(t),
                    state=state,
                    last_checked=last_checked,
                    response_ms=response_ms,
                    sla_24h_pct=_compute_sla(conn, t["id"], since_24h),
                    sla_7d_pct=_compute_sla(conn, t["id"], since_7d),
                )
            )
        return results
    finally:
        conn.close()
