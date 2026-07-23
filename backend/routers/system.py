from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection

router = APIRouter(prefix="/api/system", tags=["system"])

# Bit meanings for `vcgencmd get_throttled`, per Raspberry Pi firmware docs.
THROTTLE_BITS = {
    0: "under_voltage",
    1: "arm_freq_capped",
    2: "currently_throttled",
    3: "soft_temp_limit",
    16: "under_voltage_occurred",
    17: "arm_freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}


class ThrottleState(BaseModel):
    raw: str | None
    available: bool
    flags: dict[str, bool]


class SystemMetricOut(BaseModel):
    host: str
    timestamp: str
    cpu_pct: float | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    mem_used_pct: float | None
    temp_c: float | None
    load_1m: float | None
    throttled: ThrottleState


def decode_throttled(raw: str | None) -> ThrottleState:
    if raw is None:
        return ThrottleState(raw=None, available=False, flags={})
    try:
        value = int(raw, 16)
    except ValueError:
        return ThrottleState(raw=raw, available=False, flags={})
    flags = {name: bool(value & (1 << bit)) for bit, name in THROTTLE_BITS.items()}
    return ThrottleState(raw=raw, available=True, flags=flags)


def _row_to_metric(row) -> SystemMetricOut:
    mem_used = row["mem_used_mb"]
    mem_total = row["mem_total_mb"]
    mem_used_pct = round((mem_used / mem_total) * 100, 1) if mem_used and mem_total else None
    return SystemMetricOut(
        host=row["host"],
        timestamp=row["timestamp"],
        cpu_pct=row["cpu_pct"],
        mem_used_mb=mem_used,
        mem_total_mb=mem_total,
        mem_used_pct=mem_used_pct,
        temp_c=row["temp_c"],
        load_1m=row["load_1m"],
        throttled=decode_throttled(row["throttled_flags"]),
    )


@router.get("/metrics/latest", response_model=SystemMetricOut)
def get_latest_metric():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM system_metrics ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="No metrics collected yet")
    return _row_to_metric(row)


@router.get("/metrics/history", response_model=list[SystemMetricOut])
def get_metric_history(minutes: int = 60, limit: int = 500):
    if not 1 <= minutes <= 10080:  # cap the window at one week
        raise HTTPException(status_code=400, detail="minutes must be between 1 and 10080")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM system_metrics WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_metric(row) for row in rows]
