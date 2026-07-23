"""Per-drive storage collector: polls disk usage per mount point, writes to
disk_info. Standalone process, separate from the system metrics collector
since storage doesn't need per-second (or even per-10s) polling.

A container's own mount namespace is isolated from the host's by default,
so it only sees its own overlay filesystem plus whatever volumes were
explicitly mounted, none of the host's real drives. The Docker image
bind-mounts the host's root and /proc at /host and /host/proc so this
collector can read the host's real mount table (/host/proc/1/mountinfo,
PID 1 being the host's own init) instead of psutil.disk_partitions(),
which only reflects the calling process's own namespace. Falls back to
psutil directly when /host isn't present (bare-process dev/testing).

Run with: python -m backend.collectors.disk
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.environ.get("LARES_DISK_POLL_INTERVAL", 300))

# Pseudo/virtual filesystems that don't represent real storage to report on.
EXCLUDED_FSTYPES = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "autofs", "binfmt_misc", "debugfs",
    "mqueue", "hugetlbfs", "pstore", "securityfs", "tracefs", "configfs",
    "fusectl", "efivarfs", "nsfs", "bpf",
}

_HOST_ROOT = Path("/host")
_HOST_MOUNTINFO = _HOST_ROOT / "proc" / "1" / "mountinfo"

_INSERT_SQL = """
    INSERT INTO disk_info
        (device, mount_point, timestamp, total_gb, used_gb, free_gb, used_pct)
    VALUES
        (:device, :mount_point, :timestamp, :total_gb, :used_gb, :free_gb, :used_pct)
"""


def _read_host_partitions() -> list[tuple[str, str, str]]:
    """Parses /proc/[pid]/mountinfo format. Each line has a variable number
    of optional fields before a "-" separator, then filesystem type and
    source after it: "... mount_point ... - fstype source ..."."""
    partitions = []
    with _HOST_MOUNTINFO.open() as f:
        for line in f:
            pre, sep, post = line.partition(" - ")
            if not sep:
                continue
            pre_fields = pre.split()
            post_fields = post.split()
            if len(pre_fields) < 5 or len(post_fields) < 2:
                continue
            mount_point, fstype, device = pre_fields[4], post_fields[0], post_fields[1]
            partitions.append((device, mount_point, fstype))
    return partitions


def _iter_partitions() -> list[tuple[str, str, str]]:
    """Returns (device, mount_point, fstype) tuples: from the host's real
    mount table when containerized, otherwise from psutil directly."""
    if _HOST_MOUNTINFO.exists():
        try:
            return _read_host_partitions()
        except OSError as exc:
            logger.warning(
                "could not read host mountinfo, falling back to psutil: %s",
                type(exc).__name__,
            )
    return [(p.device, p.mountpoint, p.fstype) for p in psutil.disk_partitions(all=False)]


def collect_sample() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    samples = []
    seen_devices: set[str] = set()
    host_mode = _HOST_MOUNTINFO.exists()

    for device, mount_point, fstype in _iter_partitions():
        if fstype.lower() in EXCLUDED_FSTYPES:
            continue
        # Bind mounts of the same underlying device (duplicate host bind
        # mounts) would otherwise show up as separate "drives" with
        # identical stats; report each real device once.
        if device in seen_devices:
            continue

        usage_path = str(_HOST_ROOT / mount_point.lstrip("/")) if host_mode else mount_point
        try:
            usage = psutil.disk_usage(usage_path)
        except (PermissionError, OSError) as exc:
            logger.warning(
                "could not read usage for %s: %s", mount_point, type(exc).__name__
            )
            continue

        seen_devices.add(device)
        samples.append(
            {
                "device": device,
                "mount_point": mount_point,
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
