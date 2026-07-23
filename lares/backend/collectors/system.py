"""System metrics collector: polls psutil + vcgencmd, writes to system_metrics.

Standalone process. Run with: python -m backend.collectors.system
Writes are buffered in memory and flushed on an interval (not per-poll) to
reduce SD card wear, and flushed once more on shutdown so nothing buffered
is lost.
"""

import logging
import os
import re
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone

import psutil

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_POLL_INTERVAL", 15))
FLUSH_INTERVAL_SECONDS = float(os.environ.get("LARES_FLUSH_INTERVAL", 30))

HOSTNAME = socket.gethostname()

_VCGENCMD_WARNED = False

_INSERT_SQL = """
    INSERT INTO system_metrics
        (host, timestamp, cpu_pct, mem_used_mb, mem_total_mb, temp_c, load_1m, throttled_flags)
    VALUES
        (:host, :timestamp, :cpu_pct, :mem_used_mb, :mem_total_mb, :temp_c, :load_1m, :throttled_flags)
"""


def _run_vcgencmd(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["vcgencmd", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def get_throttled_flags() -> str | None:
    """Raw hex from `vcgencmd get_throttled`, e.g. "0x50000". Decoded at the API layer."""
    global _VCGENCMD_WARNED
    output = _run_vcgencmd("get_throttled")
    if output is None:
        if not _VCGENCMD_WARNED:
            logger.warning(
                "vcgencmd unavailable, throttle state will not be recorded "
                "(expected when not running on a Raspberry Pi)"
            )
            _VCGENCMD_WARNED = True
        return None
    match = re.search(r"0x[0-9a-fA-F]+", output)
    return match.group(0) if match else None


def get_temp_c() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except AttributeError:
        temps = {}
    for label in ("cpu_thermal", "cpu-thermal", "soc_thermal", "coretemp"):
        entries = temps.get(label)
        if entries:
            return entries[0].current

    output = _run_vcgencmd("measure_temp")
    if output:
        match = re.search(r"[\d.]+", output)
        if match:
            return float(match.group(0))
    return None


def get_load_1m() -> float | None:
    try:
        return psutil.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def collect_sample() -> dict:
    mem = psutil.virtual_memory()
    return {
        "host": HOSTNAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_pct": psutil.cpu_percent(interval=1),
        "mem_used_mb": round(mem.used / (1024 * 1024)),
        "mem_total_mb": round(mem.total / (1024 * 1024)),
        "temp_c": get_temp_c(),
        "load_1m": get_load_1m(),
        "throttled_flags": get_throttled_flags(),
    }


def flush(conn, buffer: list[dict]) -> None:
    if not buffer:
        return
    conn.executemany(_INSERT_SQL, buffer)
    conn.commit()
    logger.info("flushed %d sample(s) to system_metrics", len(buffer))
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
    logger.info(
        "collector starting (poll=%ss, flush=%ss, host=%s)",
        poll_interval, flush_interval, HOSTNAME,
    )
    try:
        while not stop:
            sample = collect_sample()
            buffer.append(sample)
            logger.debug("collected sample: %s", sample)

            if time.monotonic() - last_flush >= flush_interval:
                flush(conn, buffer)
                last_flush = time.monotonic()

            # cpu_percent(interval=1) above already blocks ~1s; sleep the remainder
            # in small increments so a signal during the wait is picked up promptly.
            remaining = poll_interval - 1
            ticks = int(remaining * 10)
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
