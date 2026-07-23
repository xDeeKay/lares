"""Docker container collector: polls the Docker Engine API, upserts current
state into `containers`, appends resource usage samples to
`container_metrics`, and periodically checks Docker Hub for image updates.

Run with: python -m backend.collectors.containers
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone

from docker.errors import DockerException, NotFound

from backend.db import get_connection, init_db
from backend.docker_client import get_docker_client
from backend.registry import check_for_update

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_CONTAINER_POLL_INTERVAL", 15))
FLUSH_INTERVAL_SECONDS = float(os.environ.get("LARES_CONTAINER_FLUSH_INTERVAL", 30))
# Registry calls are rate-limit-sensitive and not time-critical, so they run
# on their own, much longer cadence, independent of the status/stats poll.
UPDATE_CHECK_INTERVAL_SECONDS = float(os.environ.get("LARES_UPDATE_CHECK_INTERVAL", 1800))

_UPSERT_CONTAINER_SQL = """
    INSERT INTO containers (container_id, name, image, status, update_available, last_updated)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(container_id) DO UPDATE SET
        name = excluded.name,
        image = excluded.image,
        status = excluded.status,
        update_available = excluded.update_available,
        last_updated = excluded.last_updated
"""

_INSERT_METRIC_SQL = """
    INSERT INTO container_metrics
        (container_id, timestamp, cpu_pct, mem_used_mb, net_rx_bytes, net_tx_bytes)
    VALUES
        (:container_id, :timestamp, :cpu_pct, :mem_used_mb, :net_rx_bytes, :net_tx_bytes)
"""


def compute_stats(container) -> dict | None:
    try:
        stats = container.stats(stream=False)
        cpu_stats = stats["cpu_stats"]
        precpu_stats = stats["precpu_stats"]

        cpu_delta = cpu_stats["cpu_usage"]["total_usage"] - precpu_stats["cpu_usage"]["total_usage"]
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(cpu_stats["cpu_usage"].get("percpu_usage") or [1])
        cpu_pct = (cpu_delta / system_delta) * online_cpus * 100.0 if system_delta > 0 and cpu_delta > 0 else 0.0

        mem_stats = stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_detail = mem_stats.get("stats", {})
        mem_cache = mem_detail.get("cache", mem_detail.get("inactive_file", 0))
        mem_used_mb = round(max(mem_usage - mem_cache, 0) / (1024 * 1024))

        networks = stats.get("networks") or {}
        net_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
        net_tx = sum(n.get("tx_bytes", 0) for n in networks.values())

        return {
            "cpu_pct": round(cpu_pct, 2),
            "mem_used_mb": mem_used_mb,
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
        }
    except (DockerException, KeyError, TypeError, ZeroDivisionError) as exc:
        logger.debug("stats unavailable for %s: %s", container.name, type(exc).__name__)
        return None


def _resolve_update_flag(conn, container_id: str, computed: bool | None) -> bool:
    """Keep the last known flag when this poll didn't run a fresh check,
    rather than flipping it back to False every non-check cycle."""
    if computed is not None:
        return computed
    row = conn.execute(
        "SELECT update_available FROM containers WHERE container_id = ?", (container_id,)
    ).fetchone()
    return bool(row["update_available"]) if row else False


def collect_and_upsert(conn, client, due_for_update_check: bool) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    metric_samples = []

    try:
        containers = client.containers.list(all=True)
    except DockerException as exc:
        logger.error("failed to list containers: %s", type(exc).__name__)
        return metric_samples

    for c in containers:
        image_ref = c.attrs.get("Config", {}).get("Image") or c.attrs.get("Image", "unknown")

        computed_update = None
        if due_for_update_check and c.status == "running":
            try:
                local_digests = c.image.attrs.get("RepoDigests", []) if c.image else []
            except (DockerException, NotFound) as exc:
                logger.debug("could not read image digests for %s: %s", c.name, type(exc).__name__)
                local_digests = []
            computed_update = check_for_update(image_ref, local_digests)

        update_available = _resolve_update_flag(conn, c.id, computed_update)

        conn.execute(
            _UPSERT_CONTAINER_SQL,
            (c.id, c.name, image_ref, c.status, update_available, now_iso),
        )

        if c.status == "running":
            stats = compute_stats(c)
            if stats:
                metric_samples.append({"container_id": c.id, "timestamp": now_iso, **stats})

    conn.commit()
    return metric_samples


def flush_metrics(conn, buffer: list[dict]) -> None:
    if not buffer:
        return
    conn.executemany(_INSERT_METRIC_SQL, buffer)
    conn.commit()
    logger.info("flushed %d container metric sample(s)", len(buffer))
    buffer.clear()


def run(
    poll_interval: float = POLL_INTERVAL_SECONDS,
    flush_interval: float = FLUSH_INTERVAL_SECONDS,
    update_check_interval: float = UPDATE_CHECK_INTERVAL_SECONDS,
) -> None:
    init_db()
    client = get_docker_client()
    conn = get_connection()
    metrics_buffer: list[dict] = []
    stop = False

    def _handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_flush = time.monotonic()
    last_update_check = 0.0  # force an update check on the first poll

    logger.info(
        "collector starting (poll=%ss, flush=%ss, update_check=%ss)",
        poll_interval, flush_interval, update_check_interval,
    )
    try:
        while not stop:
            due_for_update_check = time.monotonic() - last_update_check >= update_check_interval

            samples = collect_and_upsert(conn, client, due_for_update_check)
            metrics_buffer.extend(samples)
            logger.debug("collected %d container(s), %d with stats", len(samples), len(samples))

            if due_for_update_check:
                last_update_check = time.monotonic()

            if time.monotonic() - last_flush >= flush_interval:
                flush_metrics(conn, metrics_buffer)
                last_flush = time.monotonic()

            ticks = int(poll_interval * 10)
            for _ in range(max(ticks, 0)):
                if stop:
                    break
                time.sleep(0.1)
    finally:
        flush_metrics(conn, metrics_buffer)
        conn.close()
        logger.info("collector stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
