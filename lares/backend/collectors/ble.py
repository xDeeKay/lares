"""BLE presence scanner: passive advertisement discovery via bleak/BlueZ,
run in a separate container (see docker-compose.yml) since bleak's Linux
backend talks to the host's bluetoothd over D-Bus, a service this project's
other containers have no reason to reach. Standalone process. Run with:
python -m backend.collectors.ble

Unlike lan.py's tick-based "scan, then sleep until next scan" model, this
collector keeps a single BleakScanner running continuously (passive
advertisement discovery only, matches the Non-goals boundary in the root
CLAUDE.md, no connecting/querying other devices) and buffers sightings in
memory, flushing to devices/device_sightings on an interval instead of
writing on every advertisement. A phone actively advertising can emit
several BLE packets per second; writing a row per packet would defeat the
entire point of batching writes to reduce SD card wear (see root CLAUDE.md's
Operational notes), so only the latest RSSI/name per mac per flush window is
kept.

Settings (flush interval) live in ble_scan_settings and are re-read every
few seconds, same pattern as lan_scan_settings, so a changed interval or a
force-flush request from the API (running in a different container) applies
without a restart.
"""

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from bleak import BleakScanner

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

FLUSH_SECONDS = float(os.environ.get("LARES_BLE_FLUSH_SECONDS", 30))
DEFAULT_FLUSH_INTERVAL_SECONDS = 30
# How often the flush loop re-checks ble_scan_settings for a changed
# interval or a force-flush request, independent of how long the actual
# flush interval currently is. Same responsiveness idea as lan.py's
# TICK_SECONDS.
SETTINGS_POLL_SECONDS = 5.0
# Backoff before retrying BleakScanner.start() after it raises (BlueZ/D-Bus
# unreachable, no adapter, adapter rfkill-blocked, etc). Never crashes the
# process, matching every other collector's "not a hard dependency" rule and
# the "handle missing/unavailable external services gracefully" quality bar.
ADAPTER_RETRY_SECONDS = 30.0

# Best-effort, deliberately not exhaustive, unlike lan.py's bundled IEEE OUI
# list: the Bluetooth SIG's company identifier registry has thousands of
# entries and isn't worth bundling here, especially since most phones
# randomize their BLE address and wouldn't carry a stable manufacturer
# association across scans anyway. Just a handful of consumer identifiers
# common enough to be worth a name in the UI.
_MANUFACTURER_IDS = {
    76: "Apple",
    6: "Microsoft",
    117: "Samsung",
    224: "Google",
    301: "Fitbit",
    89: "Nordic Semiconductor",
}

# Mutated only from _on_advertisement, which bleak/dbus-fast invokes
# synchronously on this same event loop thread (never a separate thread), so
# no lock is needed as long as nothing awaits between reading and clearing
# it in _flush below.
_buffer: dict[str, dict] = {}


def _lookup_manufacturer_vendor(manufacturer_data: dict[int, bytes]) -> str | None:
    for company_id in manufacturer_data:
        vendor = _MANUFACTURER_IDS.get(company_id)
        if vendor:
            return vendor
    return None


def _on_advertisement(device, advertisement_data) -> None:
    mac = device.address.upper()
    entry = _buffer.setdefault(mac, {})
    entry["rssi"] = advertisement_data.rssi
    name = advertisement_data.local_name or device.name
    if name:
        entry["local_name"] = name
    if "vendor" not in entry:
        vendor = _lookup_manufacturer_vendor(advertisement_data.manufacturer_data)
        if vendor:
            entry["vendor"] = vendor
    entry["last_seen"] = datetime.now(timezone.utc).isoformat()


_UPSERT_DEVICE_SQL = """
    INSERT INTO devices (mac_address, device_type, vendor, hostname, last_rssi, first_seen, last_seen)
    VALUES (:mac_address, 'ble', :vendor, :hostname, :rssi, :timestamp, :timestamp)
    ON CONFLICT(mac_address) DO UPDATE SET
        last_seen = excluded.last_seen,
        last_rssi = excluded.last_rssi,
        vendor = COALESCE(excluded.vendor, devices.vendor),
        hostname = COALESCE(excluded.hostname, devices.hostname)
"""
# device_type is deliberately left untouched on conflict, same as lan.py's
# upsert: if this mac_address already exists as a 'lan' row (a real
# possibility on hardware whose Wi-Fi and Bluetooth radios share one MAC),
# the two scanners merge into a single device rather than fighting over
# which device_type "wins". Accepted as intentional: device_sightings.rssi
# and ip_address already coexist in one shared table for exactly this
# reason. The one visible cost is that the device_type column then reflects
# whichever scanner recorded the mac first, not "every transport this
# device has been seen on", a cosmetic limitation, not a functional one.

_INSERT_SIGHTING_SQL = """
    INSERT INTO device_sightings (mac_address, timestamp, rssi, is_present)
    VALUES (:mac_address, :timestamp, :rssi, 1)
"""


def _write_flush(conn, rows: list[dict]) -> None:
    if rows:
        conn.executemany(_UPSERT_DEVICE_SQL, rows)
        conn.executemany(_INSERT_SIGHTING_SQL, rows)
    conn.execute(
        "UPDATE ble_scan_settings SET last_flush_at = ?, force_flush_requested_at = NULL WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()


def _flush(conn) -> None:
    """Deliberately synchronous, not offloaded to an executor thread: a
    sqlite3.Connection can only be used from the thread that created it
    (get_connection() is called once in _run_async, on the event loop's own
    thread), so handing conn to a ThreadPoolExecutor worker raised
    sqlite3.ProgrammingError on every flush, confirmed on real hardware. The
    writes here are a handful of rows at most every flush_interval_seconds,
    small enough that blocking the event loop for the call is a non-issue in
    practice, unlike lan.py's nmap subprocess which genuinely needs its own
    thread."""
    if not _buffer:
        _write_flush(conn, [])
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "mac_address": mac,
            "timestamp": now_iso,
            "rssi": v.get("rssi"),
            "vendor": v.get("vendor"),
            "hostname": v.get("local_name"),
        }
        for mac, v in _buffer.items()
    ]
    # No I/O between building `rows` above and clearing the buffer below, so
    # no advertisement callback (same thread, see _buffer's own docstring)
    # can interleave and have its update silently discarded mid-swap.
    _buffer.clear()
    _write_flush(conn, rows)
    logger.info("flushed %d BLE device(s)", len(rows))


def _flush_due(settings, now: datetime) -> bool:
    interval = settings["flush_interval_seconds"] or DEFAULT_FLUSH_INTERVAL_SECONDS
    last_flush_at = settings["last_flush_at"]
    if last_flush_at is None:
        return True

    last_flush = datetime.fromisoformat(last_flush_at)
    if last_flush.tzinfo is None:
        last_flush = last_flush.replace(tzinfo=timezone.utc)
    if (now - last_flush).total_seconds() >= interval:
        return True

    force_requested_at = settings["force_flush_requested_at"]
    if force_requested_at:
        force_requested = datetime.fromisoformat(force_requested_at)
        if force_requested.tzinfo is None:
            force_requested = force_requested.replace(tzinfo=timezone.utc)
        if force_requested > last_flush:
            return True

    return False


async def _flush_loop(conn, stop: asyncio.Event) -> None:
    while not stop.is_set():
        settings = conn.execute("SELECT * FROM ble_scan_settings WHERE id = 1").fetchone()
        if settings is not None and _flush_due(settings, datetime.now(timezone.utc)):
            _flush(conn)

        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _run_async(flush_seconds: float) -> None:
    conn = get_connection()
    stop = asyncio.Event()

    def _handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("collector starting (flush=%ss)", flush_seconds)
    try:
        while not stop.is_set():
            scanner = BleakScanner(detection_callback=_on_advertisement)
            try:
                await scanner.start()
            except Exception as exc:
                logger.warning(
                    "BLE adapter/D-Bus unavailable (%s), retrying in %ss",
                    type(exc).__name__,
                    ADAPTER_RETRY_SECONDS,
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=ADAPTER_RETRY_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await _flush_loop(conn, stop)
            except Exception as exc:
                # A hang/crash inside the flush loop shouldn't kill the
                # whole collector, same "can't enumerate every D-Bus/
                # subprocess failure mode up front" reasoning as lan.py's
                # _scan_bounded.
                logger.warning("BLE flush loop failed: %s", type(exc).__name__)
            finally:
                try:
                    await scanner.stop()
                except Exception:
                    pass

        _flush(conn)  # final flush of anything buffered but not yet written
    finally:
        conn.close()
        logger.info("collector stopped")


def run(flush_seconds: float = FLUSH_SECONDS) -> None:
    init_db()
    asyncio.run(_run_async(flush_seconds))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
