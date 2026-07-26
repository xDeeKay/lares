"""LAN device scanner: real ARP-based discovery via nmap, run in a separate
container with network_mode: host (see docker-compose.yml) since a bridge
network container has no L2 visibility into the physical LAN. Standalone
process. Run with: python -m backend.collectors.lan

Settings (CIDR override, scan interval) live in lan_scan_settings and are
re-read every tick, same as uptime_targets, so they apply without a restart.
force_scan_requested_at is the only way the API (running in a different
container) can nudge this collector to scan sooner, since there's no
in-process call across containers the way uptime's check-now endpoint has.
"""

import ipaddress
import logging
import os
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import nmap
import psutil

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

TICK_SECONDS = float(os.environ.get("LARES_LAN_TICK_SECONDS", 5))
DEFAULT_SCAN_INTERVAL_SECONDS = 300

_ROUTE_PATH = Path("/proc/net/route")

_NMAP_ERROR_WARNED = False
_SUBNET_DETECT_WARNED = False

_UPSERT_DEVICE_SQL = """
    INSERT INTO devices (mac_address, device_type, vendor, hostname, last_ip, first_seen, last_seen)
    VALUES (:mac_address, 'lan', :vendor, :hostname, :ip_address, :timestamp, :timestamp)
    ON CONFLICT(mac_address) DO UPDATE SET
        last_seen = excluded.last_seen,
        last_ip = excluded.last_ip,
        vendor = COALESCE(excluded.vendor, devices.vendor),
        hostname = COALESCE(excluded.hostname, devices.hostname)
"""

_INSERT_SIGHTING_SQL = """
    INSERT INTO device_sightings (mac_address, timestamp, ip_address, is_present)
    VALUES (:mac_address, :timestamp, :ip_address, 1)
"""


def _default_interface() -> str | None:
    """Parses /proc/net/route (kernel routing table) for the default route
    (Destination == 00000000), returning its interface name. This container
    runs with network_mode: host, so /proc/net/route is the host's own real
    routing table, not an isolated one, unlike disk.py's /host/proc/1/mountinfo
    bind-mount trick (that trick exists only to see past a container's own
    network namespace, which host networking already removes here)."""
    try:
        with _ROUTE_PATH.open() as f:
            lines = f.readlines()[1:]
    except OSError as exc:
        logger.warning("could not read %s: %s", _ROUTE_PATH, type(exc).__name__)
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 2:
            continue
        iface, destination = fields[0], fields[1]
        if destination == "00000000":
            return iface
    return None


def detect_cidr() -> str | None:
    """Best-effort local subnet detection: the default-route interface's
    IPv4 address + netmask, via psutil (already a project dependency)."""
    global _SUBNET_DETECT_WARNED
    iface = _default_interface()
    if iface is None:
        return None
    for addr in psutil.net_if_addrs().get(iface, []):
        if addr.family.name != "AF_INET":
            continue
        try:
            network = ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False)
        except ValueError:
            continue
        return str(network)
    if not _SUBNET_DETECT_WARNED:
        logger.warning("no IPv4 address found on default interface %s", iface)
        _SUBNET_DETECT_WARNED = True
    return None


def scan(cidr: str) -> list[dict]:
    """Runs `nmap -sn <cidr>` (ARP ping scan, real L2 discovery since this
    container has host networking) and returns one dict per live host with a
    resolved MAC address. Hosts without a MAC (nmap couldn't ARP-resolve them,
    e.g. not actually on this L2 segment) are skipped, there's nothing
    device-identifying to record for them.

    Runs the nbstat NSE script alongside the ping scan: reverse-DNS alone
    (nmap's default naming source) depends on the LAN's router/DNS actually
    populating PTR records, which many home networks don't do. NetBIOS Name
    Service gets the real computer name straight from Windows (and most
    Samba/NAS) boxes regardless of DNS, and nbstat ships with the nmap
    package already installed, no extra dependency."""
    scanner = nmap.PortScanner()
    scanner.scan(hosts=cidr, arguments="-sn --script nbstat")
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for ip in scanner.all_hosts():
        host = scanner[ip]
        if host.state() != "up":
            continue
        mac = host["addresses"].get("mac")
        if not mac:
            continue
        vendor = host.get("vendor", {}).get(mac)
        hostname = _extract_hostname(host)
        results.append(
            {
                "mac_address": mac.upper(),
                "ip_address": ip,
                "vendor": vendor,
                "hostname": hostname,
                "timestamp": now,
            }
        )
    return results


_NBSTAT_NAME_RE = re.compile(r"NetBIOS name:\s*([^\s,]+)")


def _extract_hostname(host) -> str | None:
    reverse_dns = next((h["name"] for h in host.get("hostnames", []) if h.get("name")), None)
    if reverse_dns:
        return reverse_dns
    for script in host.get("hostscript", []):
        if script.get("id") != "nbstat":
            continue
        match = _NBSTAT_NAME_RE.search(script.get("output", ""))
        if match:
            return match.group(1)
    return None


def record_scan(conn, results: list[dict]) -> None:
    if not results:
        return
    conn.executemany(_UPSERT_DEVICE_SQL, results)
    conn.executemany(_INSERT_SIGHTING_SQL, results)
    conn.commit()


def run_scan_cycle(conn, settings) -> None:
    global _NMAP_ERROR_WARNED
    cidr = settings["cidr"] or detect_cidr()
    now_iso = datetime.now(timezone.utc).isoformat()

    if cidr is None:
        logger.warning("could not resolve a subnet to scan, skipping this cycle")
        conn.execute(
            "UPDATE lan_scan_settings SET last_scan_at = ?, force_scan_requested_at = NULL WHERE id = 1",
            (now_iso,),
        )
        conn.commit()
        return

    try:
        results = scan(cidr)
    except nmap.PortScannerError as exc:
        if not _NMAP_ERROR_WARNED:
            logger.warning("nmap scan of %s failed: %s", cidr, type(exc).__name__)
            _NMAP_ERROR_WARNED = True
        results = []

    record_scan(conn, results)
    conn.execute(
        """
        UPDATE lan_scan_settings
        SET effective_cidr = ?, last_scan_at = ?, force_scan_requested_at = NULL
        WHERE id = 1
        """,
        (cidr, now_iso),
    )
    conn.commit()
    logger.info("scanned %s, found %d device(s)", cidr, len(results))


def _scan_due(settings, now: datetime) -> bool:
    interval = settings["scan_interval_seconds"] or DEFAULT_SCAN_INTERVAL_SECONDS
    last_scan_at = settings["last_scan_at"]
    if last_scan_at is None:
        return True

    last_scan = datetime.fromisoformat(last_scan_at)
    if last_scan.tzinfo is None:
        last_scan = last_scan.replace(tzinfo=timezone.utc)
    if (now - last_scan).total_seconds() >= interval:
        return True

    force_requested_at = settings["force_scan_requested_at"]
    if force_requested_at:
        force_requested = datetime.fromisoformat(force_requested_at)
        if force_requested.tzinfo is None:
            force_requested = force_requested.replace(tzinfo=timezone.utc)
        if force_requested > last_scan:
            return True

    return False


def run(tick_seconds: float = TICK_SECONDS) -> None:
    init_db()
    conn = get_connection()
    stop = False

    def _handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("collector starting (tick=%ss)", tick_seconds)
    try:
        while not stop:
            settings = conn.execute("SELECT * FROM lan_scan_settings WHERE id = 1").fetchone()
            if _scan_due(settings, datetime.now(timezone.utc)):
                run_scan_cycle(conn, settings)

            ticks = int(tick_seconds * 10)
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
