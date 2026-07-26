"""Uptime collector: polls user-configured targets (http/tcp/ping), writes
results to uptime_checks.

Standalone process. Run with: python -m backend.collectors.uptime
Targets are re-read from uptime_targets every poll cycle (not cached at
startup) since they can be added/edited/removed at runtime via the API.
Writes are buffered and flushed on an interval, same as the other collectors.
"""

import logging
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone

import requests

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_UPTIME_POLL_INTERVAL", 30))
FLUSH_INTERVAL_SECONDS = float(os.environ.get("LARES_UPTIME_FLUSH_INTERVAL", 30))
CHECK_TIMEOUT_SECONDS = float(os.environ.get("LARES_UPTIME_CHECK_TIMEOUT", 5))

_PING_MISSING_WARNED = False

_INSERT_SQL = """
    INSERT INTO uptime_checks (target_id, timestamp, is_up, response_ms)
    VALUES (:target_id, :timestamp, :is_up, :response_ms)
"""


def check_http(address: str, timeout: float) -> tuple[bool, int | None]:
    start = time.monotonic()
    try:
        resp = requests.get(address, timeout=timeout)
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return 200 <= resp.status_code < 400, elapsed_ms
    except requests.RequestException:
        return False, None


def check_tcp(address: str, timeout: float) -> tuple[bool, int | None]:
    host, _, port_str = address.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        return False, None
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, round((time.monotonic() - start) * 1000)
    except OSError:
        return False, None


def check_ping(address: str, timeout: float) -> tuple[bool | None, int | None]:
    """Returns is_up=None (not False) when the ping binary itself is missing,
    so the caller can skip recording a false "down" rather than mistaking a
    missing capability for a real outage."""
    global _PING_MISSING_WARNED
    wait_seconds = max(int(timeout), 1)
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(wait_seconds), address],
            capture_output=True,
            timeout=timeout + 2,
        )
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return result.returncode == 0, elapsed_ms
    except FileNotFoundError:
        if not _PING_MISSING_WARNED:
            logger.warning(
                "ping binary unavailable, ping-type targets will not be recorded"
            )
            _PING_MISSING_WARNED = True
        return None, None
    except subprocess.TimeoutExpired:
        return False, None


CHECKERS = {"http": check_http, "tcp": check_tcp, "ping": check_ping}


def collect_sample(target) -> dict | None:
    checker = CHECKERS.get(target["target_type"])
    if checker is None:
        logger.warning("unknown target_type %r for target %s", target["target_type"], target["name"])
        return None
    is_up, response_ms = checker(target["address"], CHECK_TIMEOUT_SECONDS)
    if is_up is None:
        return None
    return {
        "target_id": target["id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_up": is_up,
        "response_ms": response_ms,
    }


def collect_all(conn) -> list[dict]:
    targets = conn.execute("SELECT * FROM uptime_targets WHERE enabled = 1").fetchall()
    samples = []
    for target in targets:
        sample = collect_sample(target)
        if sample is not None:
            samples.append(sample)
    return samples


def flush(conn, buffer: list[dict]) -> None:
    if not buffer:
        return
    conn.executemany(_INSERT_SQL, buffer)
    conn.commit()
    logger.info("flushed %d sample(s) to uptime_checks", len(buffer))
    buffer.clear()


def run(poll_interval: float = POLL_INTERVAL_SECONDS, flush_interval: float = FLUSH_INTERVAL_SECONDS) -> None:
    init_db()
    conn = get_connection()
    buffer: list[dict] = []
    stop = False

    def _handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_flush = time.monotonic()
    logger.info("collector starting (poll=%ss, flush=%ss)", poll_interval, flush_interval)
    try:
        while not stop:
            samples = collect_all(conn)
            buffer.extend(samples)
            logger.debug("collected %d check(s)", len(samples))

            if time.monotonic() - last_flush >= flush_interval:
                flush(conn, buffer)
                last_flush = time.monotonic()

            ticks = int(poll_interval * 10)
            for _ in range(max(ticks, 0)):
                if stop:
                    break
                time.sleep(0.1)
    finally:
        flush(conn, buffer)
        conn.close()
        logger.info("collector stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
