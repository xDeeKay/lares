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


def collect_sample_bounded(target, buffer_seconds: float = 2.0) -> dict | None:
    """Runs collect_sample for one target with a hard wall-clock bound, for
    callers (like a synchronous API request handler) that can't afford to
    block indefinitely on e.g. a hanging DNS resolution (which none of the
    checkers' own `timeout` params actually bound, since getaddrinfo isn't
    covered by socket/requests timeouts). Same daemon-thread approach as the
    collector's own scheduling: a straggler thread is left running in the
    background past the deadline rather than killed, and its result is
    simply discarded when it eventually finishes."""
    result_queue: queue.Queue = queue.Queue()

    def _worker() -> None:
        result_queue.put(collect_sample(target))

    threading.Thread(target=_worker, daemon=True).start()
    try:
        return result_queue.get(timeout=_target_timeout(target) + buffer_seconds)
    except queue.Empty:
        logger.warning("manual check for target %s did not complete before the deadline", target["name"])
        return {
            "target_id": target["id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "is_up": False,
            "response_ms": None,
        }


class _PendingChecks:
    """Tracks in-flight daemon-thread checks across scheduling ticks.

    The collector's very first concurrency fix ran every due target's check
    in parallel, but still waited inline for the whole batch before the
    scheduling loop could move on: a shared deadline meant one target with
    an unusually large check_timeout_seconds override could stall every
    other target's due-check for that long too (confirmed by review, not
    just theoretical). This tracker instead separates "spawn a check" from
    "collect its result": the loop spawns a thread for anything newly due,
    then drains whatever's already finished without waiting, and gives up
    on anything that's overrun its own deadline. A slow target's thread can
    keep running across many ticks in the background without ever blocking
    the loop from re-evaluating every other target on schedule.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._since: dict[int, float] = {}
        self._targets: dict[int, object] = {}

    def pending_ids(self) -> set:
        return set(self._since)

    def is_pending(self, target_id: int) -> bool:
        return target_id in self._since

    def forget(self, target_id: int) -> None:
        """Drop bookkeeping for a target that's been deleted or disabled.
        Its thread, if still running, is orphaned but harmless."""
        self._since.pop(target_id, None)
        self._targets.pop(target_id, None)

    def spawn(self, target, now: float) -> None:
        self._since[target["id"]] = now
        self._targets[target["id"]] = target
        threading.Thread(target=self._worker, args=(target,), daemon=True).start()

    def _worker(self, target) -> None:
        self._queue.put((target["id"], collect_sample(target)))

    def drain(self) -> list[dict]:
        """Non-blocking: collects whatever results have already arrived."""
        samples: list[dict] = []
        while True:
            try:
                target_id, sample = self._queue.get_nowait()
            except queue.Empty:
                break
            self._since.pop(target_id, None)
            self._targets.pop(target_id, None)
            if sample is not None:
                samples.append(sample)
        return samples

    def expire(self, now: float) -> list[dict]:
        """Give up on anything that's run past its own timeout, recording a
        synthetic down sample and freeing its target to be checked again,
        rather than leaving it looking permanently "in flight"."""
        samples: list[dict] = []
        for target_id in list(self._since):
            target = self._targets[target_id]
            if now - self._since[target_id] > _target_timeout(target) + 2:
                logger.warning(
                    "check for target %s did not complete before its deadline, recording down",
                    target["name"],
                )
                samples.append({
                    "target_id": target_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "is_up": False,
                    "response_ms": None,
                })
                del self._since[target_id]
                del self._targets[target_id]
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
    pending = _PendingChecks()
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
            # to reuse an old rowid doesn't inherit stale scheduling state.
            current_ids = {t["id"] for t in targets}
            for stale_id in [tid for tid in last_checked if tid not in current_ids]:
                del last_checked[stale_id]
            for stale_id in pending.pending_ids() - current_ids:
                pending.forget(stale_id)

            # A target already mid-check is skipped here, not re-spawned;
            # its result (or eventual expiry) is picked up below regardless
            # of how many ticks that takes.
            due = [
                t for t in targets
                if not pending.is_pending(t["id"]) and now - last_checked.get(t["id"], 0) >= _target_interval(t)
            ]
            for t in due:
                last_checked[t["id"]] = now
                pending.spawn(t, now)

            new_samples = pending.drain() + pending.expire(now)
            buffer.extend(new_samples)
            if due or new_samples:
                logger.debug(
                    "spawned %d check(s), recorded %d sample(s) this tick",
                    len(due), len(new_samples),
                )

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
