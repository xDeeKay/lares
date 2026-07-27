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
import select
import signal
import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import nmap
import psutil
import requests
from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

from backend.db import get_connection, init_db

logger = logging.getLogger(__name__)

# Bundled fallback for when nmap's own vendor lookup (Config: nmap-mac-prefixes)
# comes back blank for a real (non-randomized) OUI it just doesn't happen to
# carry. Trimmed from the IEEE public OUI registry (standards-oui.ieee.org) to
# just the 24-bit prefix and organization name, no addresses. Lives outside
# backend/data/ deliberately: that directory is volume-mounted at runtime (see
# docker-compose.yml), so a file bundled into the image there would be shadowed
# by the empty host-side mount instead of actually being readable.
_OUI_VENDORS_PATH = Path(__file__).parent.parent / "oui" / "oui_vendors.txt"
_oui_vendors_cache: dict[str, str] | None = None
_OUI_LOAD_WARNED = False

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

# Only interfaces that look like real network hardware are auto-scanned:
# traditional/predictable Ethernet and WiFi names (eth0, end0, enp0s3,
# wlan0, wlp2s0, ...) plus a Pi-hosted-AP virtual interface (uap0/ap0). This
# is an allowlist, not a denylist of virtual interface names, after a real
# Umbrel Pi's full interface list (`ip -brief addr show`) turned up more
# virtual interfaces than any denylist could reasonably keep enumerating:
# docker0, several per-project br-* bridges, a dozen veth* pairs, tailscale0,
# and a DOWN "dind0" (Docker-in-Docker) holding an entire /16 that a
# denylist of expected names completely missed, blowing scan time up to
# 65,536 addresses and hitting SCAN_TIMEOUT_SECONDS every cycle. An
# allowlist doesn't need to anticipate every VPN/container tool's naming
# scheme in advance.
_REAL_INTERFACE_RE = re.compile(r"^(eth|en|wl|ww|uap|ap)")

_SUBNET_DETECT_WARNED = False

# SSDP/mDNS naming sources: network-wide fallbacks for whatever nbstat and
# reverse-DNS didn't resolve, e.g. TVs, media players, printers, and other
# non-Windows devices that were never going to answer a NetBIOS query.
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TIMEOUT_SECONDS = float(os.environ.get("LARES_LAN_SSDP_TIMEOUT_SECONDS", 3))
MDNS_TIMEOUT_SECONDS = float(os.environ.get("LARES_LAN_MDNS_TIMEOUT_SECONDS", 3))
MDNS_SERVICE_TYPES = [
    "_googlecast._tcp.local.",
    "_airplay._tcp.local.",
    "_raop._tcp.local.",
    "_ipp._tcp.local.",
    "_http._tcp.local.",
    "_device-info._tcp.local.",
    "_workstation._tcp.local.",
    "_smb._tcp.local.",
    "_spotify-connect._tcp.local.",
]

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


def _real_interface_addrs() -> list[tuple[str, str, str]]:
    """Returns (interface_name, ipv4_address, netmask) for every real, up
    hardware interface. Shared by detect_cidrs() (subnet auto-detection) and
    discover_ssdp_names() below, which needs to send its multicast query out
    each real interface individually, not just whichever one happens to be
    the default route, to actually reach devices on a Pi-hosted AP subnet as
    well as the main LAN."""
    stats = psutil.net_if_stats()
    addrs: list[tuple[str, str, str]] = []
    for iface, iface_addrs in psutil.net_if_addrs().items():
        if not _REAL_INTERFACE_RE.match(iface):
            continue
        if not stats.get(iface) or not stats[iface].isup:
            continue
        for addr in iface_addrs:
            if addr.family.name == "AF_INET" and addr.netmask:
                addrs.append((iface, addr.address, addr.netmask))
    return addrs


def detect_cidrs() -> list[str]:
    """Every directly-connected IPv4 subnet on a real, up hardware interface,
    via psutil (already a project dependency), not just the interface on the
    default route. A Pi that also hosts its own WiFi access point for other
    devices, a real home-lab setup, has its own subnet on that interface
    (confirmed in practice: NetworkManager's shared-connection default,
    10.42.0.0/24), distinct from the main router's LAN reached via the
    onboard Ethernet; a default-route-only detection would silently never
    scan it, missing every device connected to that AP."""
    global _SUBNET_DETECT_WARNED
    cidrs: list[str] = []
    for _iface, address, netmask in _real_interface_addrs():
        try:
            network = ipaddress.ip_network(f"{address}/{netmask}", strict=False)
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


def _parse_ssdp_location(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    for line in text.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return None


def _fetch_friendly_name(location: str) -> str | None:
    try:
        resp = requests.get(location, timeout=2)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError):
        return None
    # UPnP device description XML is namespaced
    # (urn:schemas-upnp-org:device-1-0), so match on local tag name rather
    # than requiring the exact namespace URI to be declared up front.
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == "friendlyName" and elem.text:
            return elem.text.strip()
    return None


def discover_ssdp_names(timeout: float = SSDP_TIMEOUT_SECONDS) -> dict[str, str]:
    """Sends one SSDP M-SEARCH multicast request per real interface (same
    set as _real_interface_addrs(), for the same reason detect_cidrs()
    covers every real interface: a Pi-hosted AP subnet is just as real a
    network segment as the main LAN, and a plain unbound socket's multicast
    send would only reach whichever interface the default route happens to
    pick) and collects each responder's UPnP friendlyName, keyed by IP.

    ST: upnp:rootdevice specifically, not ssdp:all: confirmed on real
    hardware that a device advertising several UPnP services at once (e.g. a
    smart TV's root device plus its separate AVTransport/MediaRenderer
    services) answers ssdp:all with multiple different LOCATION responses,
    each with its own friendlyName, one often just the raw model number.
    Since only the first response per IP is kept and UDP arrival order isn't
    stable across scans, that made the resolved name flip between scan
    cycles. Root device only gives one canonical, more human-meaningful
    name per device."""
    interface_ips = {addr for _iface, addr, _netmask in _real_interface_addrs()}
    if not interface_ips:
        return {}

    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(int(timeout), 1)}\r\n"
        "ST: upnp:rootdevice\r\n"
        "\r\n"
    ).encode("utf-8")

    locations_by_ip: dict[str, set[str]] = {}
    sockets: list[socket.socket] = []
    try:
        for local_ip in interface_ips:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip)
                )
                sock.bind((local_ip, 0))
                sock.sendto(message, SSDP_ADDR)
            except OSError as exc:
                logger.debug("SSDP send on %s failed: %s", local_ip, type(exc).__name__)
                sock.close()
                continue
            sockets.append(sock)

        deadline = time.monotonic() + timeout
        while sockets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], remaining)
            for sock in ready:
                try:
                    data, (ip, _port) = sock.recvfrom(65535)
                except OSError:
                    continue
                location = _parse_ssdp_location(data)
                if location:
                    locations_by_ip.setdefault(ip, set()).add(location)
    finally:
        for sock in sockets:
            sock.close()

    names: dict[str, str] = {}
    for ip, locations in locations_by_ip.items():
        # Confirmed on real hardware (an LG webOS TV): a single device can
        # announce several root devices at once, and not all of them
        # necessarily return valid UPnP description XML (one consistently
        # 404s/fails to parse here). Keeping only the first-captured
        # LOCATION and giving up if it happened to be the bad one made the
        # resolved name silently disappear on some cycles, falling through
        # to a different source's name instead and looking like it was
        # flip-flopping. Try every known LOCATION, in a stable sorted
        # order, until one actually parses.
        for location in sorted(locations):
            name = _fetch_friendly_name(location)
            if name:
                names[ip] = name
                break
    return names


class _MdnsNameCollector(ServiceListener):
    def __init__(self) -> None:
        self.names_by_ip: dict[str, str] = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1000)
        if info is None:
            return
        # Instance name is the part before the service type, e.g.
        # "Living Room TV._googlecast._tcp.local." -> "Living Room TV".
        instance = name.removesuffix(f".{type_}") or name
        for ip in info.parsed_addresses(IPVersion.V4Only):
            self.names_by_ip.setdefault(ip, instance)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover_mdns_names(timeout: float = MDNS_TIMEOUT_SECONDS) -> dict[str, str]:
    """Browses a fixed list of common mDNS service types (Chromecast,
    AirPlay, printers, generic host advertisement, ...) once per scan cycle
    and returns each responder's advertised instance name keyed by IP.
    Zeroconf binds every real interface by default (unlike raw SSDP above,
    which needs that handled manually), so no extra multi-subnet handling
    is needed here."""
    zc = Zeroconf()
    listener = _MdnsNameCollector()
    try:
        ServiceBrowser(zc, MDNS_SERVICE_TYPES, listener=listener)
        time.sleep(timeout)
    except OSError as exc:
        logger.debug("mDNS discovery failed: %s", type(exc).__name__)
    finally:
        zc.close()
    return listener.names_by_ip


def _discover_fallback_names() -> dict[str, str]:
    """Runs SSDP and mDNS discovery concurrently, since neither depends on
    the other and both are pure listen-and-wait I/O, not worth paying their
    timeouts back to back. SSDP wins on a collision: a UPnP friendlyName
    tends to be a more deliberately user-facing device name than an mDNS
    service instance name."""
    results: dict[str, dict[str, str]] = {}

    def _run(key: str, fn) -> None:
        results[key] = fn()

    threads = [
        threading.Thread(target=_run, args=("mdns", discover_mdns_names), daemon=True),
        threading.Thread(target=_run, args=("ssdp", discover_ssdp_names), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    merged = dict(results.get("mdns", {}))
    merged.update(results.get("ssdp", {}))
    return merged


def _load_oui_vendors() -> dict[str, str]:
    global _oui_vendors_cache, _OUI_LOAD_WARNED
    if _oui_vendors_cache is not None:
        return _oui_vendors_cache
    vendors: dict[str, str] = {}
    try:
        with _OUI_VENDORS_PATH.open(encoding="utf-8") as f:
            for line in f:
                prefix, _, vendor = line.rstrip().partition("\t")
                if prefix and vendor:
                    vendors[prefix] = vendor
    except OSError as exc:
        if not _OUI_LOAD_WARNED:
            logger.warning("could not load bundled OUI vendor list: %s", type(exc).__name__)
            _OUI_LOAD_WARNED = True
    _oui_vendors_cache = vendors
    return vendors


def _lookup_vendor(mac: str) -> str | None:
    """Fallback for when nmap's own vendor lookup comes back blank. Only
    meaningful for a real, globally-assigned OUI; a randomized/locally-
    administered MAC (common on phones for privacy) has no vendor to find,
    no database will ever fix that, so this is a genuine ceiling, not a
    coverage gap."""
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return _load_oui_vendors().get(prefix)


def scan(cidr: str) -> list[dict]:
    """Runs an ARP ping scan (-PR, real L2 discovery since this container has
    host networking) and returns one dict per live host with a resolved MAC
    address. Hosts without a MAC (nmap couldn't ARP-resolve them, e.g. not
    actually on this L2 segment) are skipped, there's nothing
    device-identifying to record for them.

    Also runs the nbstat NSE script: reverse-DNS alone (nmap's default naming
    source) depends on the LAN's router/DNS actually populating PTR records,
    which many home networks don't do. NetBIOS Name Service gets the real
    computer name straight from Windows (and most Samba/NAS) boxes regardless
    of DNS, and nbstat ships with the nmap package already installed, no
    extra dependency.

    Confirmed via --packet-trace on real hardware: nbstat's hostrule needs
    UDP port 137 to already have a probed state before it'll send anything,
    and plain -sn (host discovery only, no port scan at all) never gives it
    one, silently no-opping the script every time regardless of whether the
    target would have answered. -sU -p137 is nmap's own documented way to
    invoke this script: a real but minimal single-port UDP scan, just enough
    to satisfy that precondition, not a full port scan.

    --host-timeout bounds how long nmap itself will wait on any single
    unresponsive host (defense in depth alongside _scan_bounded's outer
    wall-clock timeout below, which is the real backstop).

    NetBIOS/reverse-DNS only ever resolve Windows/Samba boxes and
    DNS-registered hosts respectively, missing most of a typical home
    network (TVs, media players, printers, IoT gear). SSDP and mDNS are
    tried next, network-wide rather than per-host, as a fallback for
    whatever's still unnamed after the per-host lookups above."""
    scanner = nmap.PortScanner()
    scanner.scan(hosts=cidr, arguments="-PR -sU -p137 --script nbstat --host-timeout 15s")
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for ip in scanner.all_hosts():
        host = scanner[ip]
        if host.state() != "up":
            continue
        mac = host["addresses"].get("mac")
        if not mac:
            continue
        vendor = host.get("vendor", {}).get(mac) or _lookup_vendor(mac)
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

    if any(r["hostname"] is None for r in results):
        fallback_names = _discover_fallback_names()
        for r in results:
            if r["hostname"] is None:
                r["hostname"] = fallback_names.get(r["ip_address"])

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
