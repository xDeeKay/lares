import os
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.collectors.uptime import collect_sample_bounded
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
# How many consecutive failing checks are required before a target flips
# from "pending" (just started failing, might be a blip) to a confirmed
# "down". Recovery is immediate: a single "up" check clears it right away.
UPTIME_DEBOUNCE_COUNT = int(os.environ.get("LARES_UPTIME_DEBOUNCE_COUNT", 2))

TargetType = Literal["http", "tcp", "ping"]
UptimeState = Literal["up", "down", "pending", "stale", "unknown"]

_INSERT_CHECK_SQL = """
    INSERT INTO uptime_checks (target_id, timestamp, is_up, response_ms)
    VALUES (:target_id, :timestamp, :is_up, :response_ms)
"""


class UptimeTargetOut(BaseModel):
    id: int
    name: str
    target_type: TargetType
    address: str
    enabled: bool
    created_at: str
    check_interval_seconds: int | None
    check_timeout_seconds: int | None


class UptimeTargetCreate(BaseModel):
    name: str
    target_type: TargetType
    address: str
    enabled: bool = True
    check_interval_seconds: int | None = None
    check_timeout_seconds: int | None = None


class UptimeTargetUpdate(BaseModel):
    name: str | None = None
    target_type: TargetType | None = None
    address: str | None = None
    enabled: bool | None = None
    check_interval_seconds: int | None = None
    check_timeout_seconds: int | None = None
    # Explicit flags so a PATCH can clear an override back to "use the
    # global default" (None), which a plain "field not provided" can't
    # distinguish from "leave it as-is".
    clear_check_interval: bool = False
    clear_check_timeout: bool = False


class UptimeStatusOut(BaseModel):
    target: UptimeTargetOut
    state: UptimeState
    last_checked: str | None
    response_ms: int | None
    sla_24h_pct: float | None
    sla_7d_pct: float | None


class UptimeCheckOut(BaseModel):
    timestamp: str
    is_up: bool
    response_ms: int | None


class UptimeIncidentOut(BaseModel):
    started_at: str
    ended_at: str | None  # null means still ongoing
    duration_seconds: float


def _validate_name(name: str) -> None:
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")


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
        if "://" in address:
            raise HTTPException(
                status_code=400,
                detail='tcp targets must be "host:port", not a URL (no scheme like http://)',
            )
        host, sep, port_str = address.rpartition(":")
        if not sep or not host:
            raise HTTPException(status_code=400, detail='tcp targets must be "host:port"')
        try:
            port = int(port_str)
        except ValueError:
            port = -1
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=400, detail="tcp port must be between 1 and 65535")
    elif target_type == "ping":
        if "://" in address:
            raise HTTPException(
                status_code=400,
                detail="ping targets must be a bare hostname or IP, not a URL",
            )
        if address.strip().startswith("-"):
            raise HTTPException(
                status_code=400,
                detail='ping targets must not start with "-" (would be parsed as a ping option)',
            )


def _row_to_target(row) -> UptimeTargetOut:
    return UptimeTargetOut(
        id=row["id"],
        name=row["name"],
        target_type=row["target_type"],
        address=row["address"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        check_interval_seconds=row["check_interval_seconds"],
        check_timeout_seconds=row["check_timeout_seconds"],
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


def _resolve_state(conn, target_id: int, latest, now: datetime) -> tuple[UptimeState, str | None, int | None]:
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

    if latest["is_up"]:
        return "up", timestamp_str, response_ms

    # Latest check failed. Only confirm "down" after UPTIME_DEBOUNCE_COUNT
    # consecutive failures, so a single transient blip shows as "pending"
    # rather than immediately alarming.
    recent = conn.execute(
        "SELECT is_up FROM uptime_checks WHERE target_id = ? ORDER BY timestamp DESC LIMIT ?",
        (target_id, UPTIME_DEBOUNCE_COUNT),
    ).fetchall()
    if len(recent) < UPTIME_DEBOUNCE_COUNT or any(r["is_up"] for r in recent):
        return "pending", timestamp_str, response_ms
    return "down", timestamp_str, response_ms


def _status_for_target(conn, target_row, now: datetime) -> UptimeStatusOut:
    latest = conn.execute(
        "SELECT * FROM uptime_checks WHERE target_id = ? ORDER BY timestamp DESC LIMIT 1",
        (target_row["id"],),
    ).fetchone()
    since_24h = (now - timedelta(hours=24)).isoformat()
    since_7d = (now - timedelta(days=7)).isoformat()
    state, last_checked, response_ms = _resolve_state(conn, target_row["id"], latest, now)
    return UptimeStatusOut(
        target=_row_to_target(target_row),
        state=state,
        last_checked=last_checked,
        response_ms=response_ms,
        sla_24h_pct=_compute_sla(conn, target_row["id"], since_24h),
        sla_7d_pct=_compute_sla(conn, target_row["id"], since_7d),
    )


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
    _validate_name(payload.name)
    _validate_address(payload.target_type, payload.address)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO uptime_targets
                (name, target_type, address, enabled, created_at,
                 check_interval_seconds, check_timeout_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.target_type,
                payload.address,
                payload.enabled,
                datetime.now(timezone.utc).isoformat(),
                payload.check_interval_seconds,
                payload.check_timeout_seconds,
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

        if payload.clear_check_interval:
            check_interval = None
        elif payload.check_interval_seconds is not None:
            check_interval = payload.check_interval_seconds
        else:
            check_interval = row["check_interval_seconds"]

        if payload.clear_check_timeout:
            check_timeout = None
        elif payload.check_timeout_seconds is not None:
            check_timeout = payload.check_timeout_seconds
        else:
            check_timeout = row["check_timeout_seconds"]

        if payload.name is not None:
            _validate_name(name)
        if payload.address is not None or payload.target_type is not None:
            _validate_address(target_type, address)

        conn.execute(
            """
            UPDATE uptime_targets
            SET name = ?, target_type = ?, address = ?, enabled = ?,
                check_interval_seconds = ?, check_timeout_seconds = ?
            WHERE id = ?
            """,
            (name, target_type, address, enabled, check_interval, check_timeout, target_id),
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


@router.post("/targets/{target_id}/check", response_model=UptimeStatusOut)
def check_target_now(target_id: int):
    """Runs one check immediately against the live target, bypassing the
    collector's own schedule, and records the result like a normal poll
    would. Useful right after adding a target instead of waiting out the
    stale window to see whether it's actually reachable.

    Uses collect_sample_bounded (not collect_sample directly): this runs
    inline in a synchronous request handler, which FastAPI dispatches to
    its shared default thread pool, so a single hung DNS resolution here
    (not bounded by the checker's own timeout, only connect/read are)
    could otherwise pin one of a limited number of worker threads well
    past the configured timeout and, with a couple of overlapping requests,
    starve other endpoints of pool capacity.
    """
    conn = get_connection()
    try:
        target = conn.execute("SELECT * FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="Target not found")

        sample = collect_sample_bounded(target)
        if sample is not None:
            conn.execute(_INSERT_CHECK_SQL, sample)
            conn.commit()

        return _status_for_target(conn, target, datetime.now(timezone.utc))
    finally:
        conn.close()


@router.get("/targets/{target_id}/history", response_model=list[UptimeCheckOut])
def get_target_history(target_id: int, hours: int = 24, limit: int = 2000):
    if not 1 <= hours <= 24 * 30:  # cap the window at 30 days
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    conn = get_connection()
    try:
        target = conn.execute("SELECT id FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="Target not found")

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """
            SELECT timestamp, is_up, response_ms FROM uptime_checks
            WHERE target_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC LIMIT ?
            """,
            (target_id, since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        UptimeCheckOut(timestamp=r["timestamp"], is_up=bool(r["is_up"]), response_ms=r["response_ms"])
        for r in rows
    ]


MAX_INCIDENT_ROWS = 20000


@router.get("/targets/{target_id}/incidents", response_model=list[UptimeIncidentOut])
def get_target_incidents(target_id: int, hours: int = 24 * 7):
    if not 1 <= hours <= 24 * 30:  # cap the window at 30 days
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")

    conn = get_connection()
    try:
        target = conn.execute("SELECT id FROM uptime_targets WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="Target not found")

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # A target with a short custom check_interval_seconds over the full
        # 30-day window could otherwise mean scanning hundreds of thousands
        # of rows in Python. Cap to the most recent MAX_INCIDENT_ROWS (query
        # DESC + LIMIT, then reverse back to ascending) rather than the
        # oldest rows in the window, since recent incidents are what this
        # endpoint is actually for.
        rows_desc = conn.execute(
            """
            SELECT timestamp, is_up FROM uptime_checks
            WHERE target_id = ? AND timestamp >= ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (target_id, since, MAX_INCIDENT_ROWS),
        ).fetchall()
        rows = list(reversed(rows_desc))
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    incidents: list[UptimeIncidentOut] = []
    current_start: str | None = None
    for row in rows:
        if not row["is_up"] and current_start is None:
            current_start = row["timestamp"]
        elif row["is_up"] and current_start is not None:
            incidents.append(_build_incident(current_start, row["timestamp"], now))
            current_start = None
    if current_start is not None:
        incidents.append(_build_incident(current_start, None, now))
    incidents.reverse()  # most recent first
    return incidents


def _build_incident(started_at: str, ended_at: str | None, now: datetime) -> UptimeIncidentOut:
    def _parse(ts: str) -> datetime:
        parsed = datetime.fromisoformat(ts)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    start_dt = _parse(started_at)
    end_dt = _parse(ended_at) if ended_at else now
    duration_seconds = max((end_dt - start_dt).total_seconds(), 0)
    return UptimeIncidentOut(started_at=started_at, ended_at=ended_at, duration_seconds=duration_seconds)


@router.get("/status", response_model=list[UptimeStatusOut])
def get_uptime_status():
    conn = get_connection()
    try:
        targets = conn.execute("SELECT * FROM uptime_targets ORDER BY name").fetchall()
        now = datetime.now(timezone.utc)
        return [_status_for_target(conn, t, now) for t in targets]
    finally:
        conn.close()
