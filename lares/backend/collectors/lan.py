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
import queue
import re
import signal
import threading
import time
from datetime import datetime, timezone

import nmap
import psutil

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

TICK_SECONDS = float(os.environ.get("LARES_LAN_TICK_SECONDS", 5))
DEFAULT_SCAN_INTERVAL_SECONDS = 300
# Hard backstop around the whole nmap invocation, not just its own
# --host-timeout below. Confirmed on real hardware: a single host that
# silently drops NBNS (UDP 137) return traffic instead of sending an ICMP
# unreachable can leave the underlying nmap subprocess blocked past its own
# timeout handling, and since this collector is single-threaded with nothing
# else running concurrently, that hangs the entire loop forever, no more
# scans, no more force_scan_requested_at handling, until the container is
# manually restarted.
SCAN_TIMEOUT_SECONDS = float(os.environ.get("LARES_LAN_SCAN_TIMEOUT_SECONDS", 120))

# Interfaces excluded from auto-detection: a Pi running several Umbrel apps
# inevitably has plenty of these (docker0, one veth per container, the
# umbrel_main_network bridge, ...), and they're internal container networks,
# not real LAN segments. Scanning them would just surface other containers'
# internal IPs instead of actual devices.
_EXCLUDED_INTERFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "wg", "cni")

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


def detect_cidrs() -> list[str]:
    """Every directly-connected IPv4 subnet on a non-virtual interface, via
    psutil (already a project dependency), not just the interface on the
    default route. A Pi that also hosts its own WiFi access point for other
    devices, a real home-lab setup, has its own subnet on that interface,
    distinct from the main router's LAN reached via eth0; a default-route-only
    detection would silently never scan it, missing every device connected to
    that AP."""
    global _SUBNET_DETECT_WARNED
    cidrs: list[str] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith(_EXCLUDED_INTERFACE_PREFIXES):
            continue
        for addr in addrs:
            if addr.family.name != "AF_INET" or not addr.netmask:
                continue
            try:
                network = ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False)
            except ValueError:
                continue
            if network.prefixlen >= 31:  # point-to-point/no real subnet to scan
                continue
            cidr = str(network)
            if cidr not in cidrs:
                cidrs.append(cidr)
    if not cidrs and not _SUBNET_DETECT_WARNED:
        logger.warning("no usable IPv4 subnet found on any non-virtual interface")
        _SUBNET_DETECT_WARNED = True
    return cidrs


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
    package already installed, no extra dependency.

    --host-timeout bounds how long nmap itself will wait on any single
    unresponsive host (defense in depth alongside _scan_bounded's outer
    wall-clock timeout below, which is the real backstop)."""
    scanner = nmap.PortScanner()
    scanner.scan(hosts=cidr, arguments="-sn --script nbstat --host-timeout 15s")
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


def _scan_bounded(cidr: str, timeout_seconds: float) -> list[dict]:
    """Runs scan() with a hard wall-clock bound in a daemon thread, same
    approach as uptime.py's collect_sample_bounded for the identical class
    of problem: a hang inside the underlying subprocess that its own
    timeout arguments don't fully cover. Catches any exception, not just
    nmap.PortScannerError, since a subprocess/XML-parsing pipeline like this
    has failure modes that can't all be enumerated up front, and none of
    them should be allowed to kill this collector's main loop, silently
    ending LAN scanning entirely rather than just failing one cycle. A
    straggler thread past the deadline is left running in the background
    rather than killed; its result is simply discarded when it eventually
    finishes."""
    result_queue: queue.Queue = queue.Queue()

    def _worker() -> None:
        try:
            result_queue.put(("ok", scan(cidr)))
        except Exception as exc:
            result_queue.put(("error", exc))

    threading.Thread(target=_worker, daemon=True).start()
    try:
        status, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        logger.warning(
            "scan of %s did not complete within %ss, skipping this cycle", cidr, timeout_seconds
        )
        return []
    if status == "error":
        logger.warning("nmap scan of %s failed: %s", cidr, type(payload).__name__)
        return []
    return payload


def record_scan(conn, results: list[dict]) -> None:
    if not results:
        return
    conn.executemany(_UPSERT_DEVICE_SQL, results)
    conn.executemany(_INSERT_SIGHTING_SQL, results)
    conn.commit()


def run_scan_cycle(conn, settings) -> None:
    # nmap accepts multiple space-separated target specs in one invocation
    # (`nmap -sn 192.168.1.0/24 192.168.4.0/24`), so covering every detected
    # subnet is just a matter of joining them, no per-subnet scan needed.
    cidr = settings["cidr"] or " ".join(detect_cidrs()) or None
    now_iso = datetime.now(timezone.utc).isoformat()

    if cidr is None:
        logger.warning("could not resolve a subnet to scan, skipping this cycle")
        conn.execute(
            "UPDATE lan_scan_settings SET last_scan_at = ?, force_scan_requested_at = NULL WHERE id = 1",
            (now_iso,),
        )
        conn.commit()
        return

    results = _scan_bounded(cidr, SCAN_TIMEOUT_SECONDS)
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
