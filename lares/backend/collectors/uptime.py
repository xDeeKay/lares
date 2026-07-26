"""Uptime collector: polls user-configured targets (http/tcp/ping), writes
results to uptime_checks.

Standalone process. Run with: python -m backend.collectors.uptime
Targets are re-read from uptime_targets every scheduling tick (not cached at
startup) since they can be added/edited/removed at runtime via the API, and
each target can override the global poll interval/timeout individually.
Checks run concurrently (not sequentially) so one slow/hanging target can't
delay every other target's check for the cycle. Writes are buffered and
flushed on an interval, same as the other collectors.
"""

import logging
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

import requests

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

# Fine-grained scheduling tick: how often the loop re-checks which targets
# are due, independent of any individual target's own poll interval.
TICK_SECONDS = float(os.environ.get("LARES_UPTIME_TICK_SECONDS", 5))
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
            # "--" stops ping from ever interpreting an address starting
            # with "-" as an option, defense-in-depth alongside the
            # leading-dash rejection already done at validation time.
            ["ping", "-c", "1", "-W", str(wait_seconds), "--", address],
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


def _target_timeout(target) -> float:
    override = target["check_timeout_seconds"]
    return float(override) if override else CHECK_TIMEOUT_SECONDS


def _target_interval(target) -> float:
    override = target["check_interval_seconds"]
    return float(override) if override else POLL_INTERVAL_SECONDS


def collect_sample(target) -> dict | None:
    checker = CHECKERS.get(target["target_type"])
    if checker is None:
        logger.warning("unknown target_type %r for target %s", target["target_type"], target["name"])
        return None

    timeout = _target_timeout(target)
    try:
        is_up, response_ms = checker(target["address"], timeout)
    except Exception as exc:
        # A checker escaping its own exception handling (e.g. subprocess
        # raising a bare OSError under fd/memory pressure, not just the
        # FileNotFoundError/TimeoutExpired check_ping already catches) must
        # not be allowed to propagate into the collector's main loop and
        # kill the whole process, silently ending monitoring for every
        # target, not just this one.
        logger.warning(
            "check for target %s (%s) failed unexpectedly: %s",
            target["name"], target["target_type"], type(exc).__name__,
        )
        return None

    if is_up is None:
        return None
    return {
        "target_id": target["id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_up": is_up,
        "response_ms": response_ms,
    }


def collect_concurrent(targets: list) -> list[dict]:
    """Runs collect_sample for every due target in parallel, so one slow or
    hanging check can't delay the others.

    Uses raw daemon threads + a shared queue rather than ThreadPoolExecutor:
    a pool's context manager (or explicit shutdown(wait=True)) blocks on
    __exit__ until every submitted thread finishes, which would silently
    undo the whole point of this function (confirmed by an actual timing
    test: a per-future timeout inside the loop did not stop the overall
    call from blocking on a slow target for its full duration). Daemon
    threads carry no such join-on-exit obligation: a straggler simply
    keeps running in the background past this function's own deadline,
    and its result is discarded when it eventually finishes.
    """
    if not targets:
        return []

    results: queue.Queue = queue.Queue()

    def _worker(target) -> None:
        results.put((target["id"], collect_sample(target)))

    for t in targets:
        threading.Thread(target=_worker, args=(t,), daemon=True).start()

    deadline = time.monotonic() + max(_target_timeout(t) for t in targets) + 2
    received: dict[int, dict | None] = {}
    while len(received) < len(targets):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            target_id, sample = results.get(timeout=min(remaining, 0.5))
            received[target_id] = sample
        except queue.Empty:
            continue

    samples: list[dict] = []
    for t in targets:
        if t["id"] in received:
            sample = received[t["id"]]
        else:
            logger.warning(
                "check for target %s did not complete before the deadline, recording down",
                t["name"],
            )
            sample = {
                "target_id": t["id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "is_up": False,
                "response_ms": None,
            }
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


def run(
    tick_seconds: float = TICK_SECONDS,
    flush_interval: float = FLUSH_INTERVAL_SECONDS,
) -> None:
    init_db()
    conn = get_connection()
    buffer: list[dict] = []
    last_checked: dict[int, float] = {}
    stop = False

    def _handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_flush = time.monotonic()
    logger.info(
        "collector starting (tick=%ss, default poll=%ss, flush=%ss)",
        tick_seconds, POLL_INTERVAL_SECONDS, flush_interval,
    )
    try:
        while not stop:
            now = time.monotonic()
            targets = conn.execute("SELECT * FROM uptime_targets WHERE enabled = 1").fetchall()

            # Drop bookkeeping for targets that are no longer enabled (or no
            # longer exist), so a deleted-then-recreated target that happens
            # to reuse an old rowid doesn't inherit a stale last-checked time.
            current_ids = {t["id"] for t in targets}
            for stale_id in [tid for tid in last_checked if tid not in current_ids]:
                del last_checked[stale_id]

            due = [t for t in targets if now - last_checked.get(t["id"], 0) >= _target_interval(t)]
            for t in due:
                last_checked[t["id"]] = now

            if due:
                samples = collect_concurrent(due)
                buffer.extend(samples)
                logger.debug("checked %d target(s), %d sample(s) recorded", len(due), len(samples))

            if time.monotonic() - last_flush >= flush_interval:
                flush(conn, buffer)
                last_flush = time.monotonic()

            ticks = int(tick_seconds * 10)
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
