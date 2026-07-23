import logging
from datetime import datetime, timedelta, timezone

from docker.errors import DockerException, NotFound
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import get_connection
from backend.docker_client import get_docker_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/containers", tags=["containers"])

# Only stop/restart are exposed here, deliberately (see the two endpoints at
# the bottom of this file). No remove/prune/exec from the UI: these are the
# only write actions Lares takes against the rest of the homelab, and every
# one is logged to container_actions.


class ContainerOut(BaseModel):
    container_id: str
    name: str
    image: str
    status: str
    update_available: bool
    last_updated: str


class ContainerMetricOut(BaseModel):
    container_id: str
    timestamp: str
    cpu_pct: float | None
    mem_used_mb: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None


class ContainerActionOut(BaseModel):
    container_id: str
    action: str
    timestamp: str
    success: bool
    status: str | None  # the container's resulting status, so the frontend can
    # update its view immediately rather than waiting for the next poll


class ContainerLogsOut(BaseModel):
    container_id: str
    lines: list[str]


class ActionConfirm(BaseModel):
    confirm: bool = False


def _row_to_container(row) -> ContainerOut:
    return ContainerOut(
        container_id=row["container_id"],
        name=row["name"],
        image=row["image"],
        status=row["status"],
        update_available=bool(row["update_available"]),
        last_updated=row["last_updated"],
    )


def _row_to_container_metric(row) -> ContainerMetricOut:
    return ContainerMetricOut(
        container_id=row["container_id"],
        timestamp=row["timestamp"],
        cpu_pct=row["cpu_pct"],
        mem_used_mb=row["mem_used_mb"],
        net_rx_bytes=row["net_rx_bytes"],
        net_tx_bytes=row["net_tx_bytes"],
    )


def _row_to_action(row) -> ContainerActionOut:
    return ContainerActionOut(
        container_id=row["container_id"],
        action=row["action"],
        timestamp=row["timestamp"],
        success=bool(row["success"]),
    )


def _get_docker_client_or_503():
    try:
        return get_docker_client()
    except DockerException as exc:
        logger.error("docker client unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Docker is unavailable")


def _log_action(container_id: str, action: str, timestamp: str, success: bool) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO container_actions (container_id, action, timestamp, success) VALUES (?, ?, ?, ?)",
            (container_id, action, timestamp, success),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("", response_model=list[ContainerOut])
def list_containers():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM containers ORDER BY name").fetchall()
    finally:
        conn.close()
    return [_row_to_container(row) for row in rows]


@router.get("/actions", response_model=list[ContainerActionOut])
def get_action_history(limit: int = 200):
    if not 1 <= limit <= 2000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 2000")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM container_actions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_action(row) for row in rows]


@router.get("/{container_id}/metrics/history", response_model=list[ContainerMetricOut])
def get_container_metric_history(container_id: str, minutes: int = 60, limit: int = 500):
    if not 1 <= minutes <= 10080:  # cap the window at one week
        raise HTTPException(status_code=400, detail="minutes must be between 1 and 10080")
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM container_metrics WHERE container_id = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (container_id, since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_container_metric(row) for row in rows]


@router.get("/{container_id}/logs", response_model=ContainerLogsOut)
def get_container_logs(container_id: str, tail: int = 200):
    if not 1 <= tail <= 2000:
        raise HTTPException(status_code=400, detail="tail must be between 1 and 2000")

    client = _get_docker_client_or_503()
    try:
        container = client.containers.get(container_id)
        raw = container.logs(tail=tail, timestamps=True)
    except NotFound:
        raise HTTPException(status_code=404, detail="Container not found")
    except DockerException as exc:
        logger.error("log fetch failed for %s: %s", container_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Failed to fetch logs")

    text = raw.decode("utf-8", errors="replace")
    return ContainerLogsOut(container_id=container_id, lines=text.splitlines())


def _perform_action(container_id: str, action: str, body: ActionConfirm) -> ContainerActionOut:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Action requires confirm: true in the request body")

    client = _get_docker_client_or_503()
    timestamp = datetime.now(timezone.utc).isoformat()
    success = False
    error_detail: str | None = None
    new_status: str | None = None

    try:
        container = client.containers.get(container_id)
        if action == "stop":
            container.stop(timeout=10)
        else:
            container.restart(timeout=10)
        success = True
    except NotFound:
        error_detail = "Container not found"
    except DockerException as exc:
        logger.error("%s failed for %s: %s", action, container_id, type(exc).__name__)
        error_detail = f"Docker {action} failed"

    if success:
        # Best-effort: the action itself already succeeded above, so a
        # failure here (fetching fresh state) shouldn't flip the result to
        # failed, it just means new_status stays None and the frontend
        # falls back to its next regular poll to see the updated status.
        try:
            container.reload()
            new_status = container.status
        except DockerException as exc:
            logger.warning("could not refresh status for %s after %s: %s", container_id, action, type(exc).__name__)

    _log_action(container_id, action, timestamp, success)

    if success and new_status is not None:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE containers SET status = ?, last_updated = ? WHERE container_id = ?",
                (new_status, timestamp, container_id),
            )
            conn.commit()
        finally:
            conn.close()

    if not success:
        status_code = 404 if error_detail == "Container not found" else 502
        raise HTTPException(status_code=status_code, detail=error_detail)

    return ContainerActionOut(
        container_id=container_id, action=action, timestamp=timestamp, success=success, status=new_status
    )


@router.post("/{container_id}/stop", response_model=ContainerActionOut)
def stop_container(container_id: str, body: ActionConfirm):
    return _perform_action(container_id, "stop", body)


@router.post("/{container_id}/restart", response_model=ContainerActionOut)
def restart_container(container_id: str, body: ActionConfirm):
    return _perform_action(container_id, "restart", body)
