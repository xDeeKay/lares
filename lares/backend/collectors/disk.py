"""Per-drive storage collector: polls psutil.disk_usage() per mount point,
writes to disk_info. Standalone process, separate from the system metrics
collector since storage doesn't need per-second (or even per-10s) polling.

Run with: python -m backend.collectors.disk
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone

import psutil

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_DISK_POLL_INTERVAL", 300))

# Pseudo/virtual filesystems that don't represent real storage to report on.
EXCLUDED_FSTYPES = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "autofs", "binfmt_misc", "debugfs",
    "mqueue", "hugetlbfs", "pstore", "securityfs", "tracefs", "configfs",
    "fusectl", "efivarfs",
}

_INSERT_SQL = """
    INSERT INTO disk_info
        (device, mount_point, timestamp, total_gb, used_gb, free_gb, used_pct)
    VALUES
        (:device, :mount_point, :timestamp, :total_gb, :used_gb, :free_gb, :used_pct)
"""


def collect_sample() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    samples = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in EXCLUDED_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError) as exc:
            logger.warning(
                "could not read usage for %s: %s", part.mountpoint, type(exc).__name__
            )
            continue
        samples.append(
            {
                "device": part.device,
                "mount_point": part.mountpoint,
                "timestamp": now,
                "total_gb": round(usage.total / (1024**3), 2),
                "used_gb": round(usage.used / (1024**3), 2),
                "free_gb": round(usage.free / (1024**3), 2),
                "used_pct": usage.percent,
            }
        )
    return samples


def run(poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    init_db()
    conn = get_connection()
    stop = False

    def _handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("collector starting (poll=%ss)", poll_interval)
    try:
        while not stop:
            samples = collect_sample()
            if samples:
                conn.executemany(_INSERT_SQL, samples)
                conn.commit()
                logger.info(
                    "wrote %d drive(s): %s",
                    len(samples),
                    ", ".join(s["mount_point"] for s in samples),
                )
            else:
                logger.warning("no drives found to report on")

            ticks = int(poll_interval * 10)
            for _ in range(max(ticks, 0)):
                if stop:
                    break
                time.sleep(0.1)
    finally:
        conn.close()
        logger.info("collector stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
