# Lares for Umbrel

<p align="center">
  <img src="lares/cornucopia.svg" width="170" alt="Lares cornucopia mark">
</p>

A custom, from-scratch monitoring dashboard for a home lab running on Umbrel. Tracks system resources, per-drive storage, Docker container status, service uptime, and devices on your LAN, with stop/restart control and image update checks built in, all from one dashboard instead of stitching together separate tools.

## Requirements

- Umbrel OS
- Raspberry Pi 5 (arm64), or any arm64 host running Umbrel

## Install

1. Open your Umbrel dashboard
2. Go to **App Store → Community App Stores**
3. Click **Add Community App Store** and paste:
   ```
   https://github.com/xDeeKay/lares
   ```
4. Find **Lares** and click **Install**
5. Open the app and set a password on first launch, there's no default credential

## What it monitors

| Feature | Detail |
|---|---|
| System | CPU, memory, load average, and temperature, polled every 15s. Surfaces `vcgencmd`'s throttle/undervoltage state when available, since a Pi can throttle silently under thermal or power stress with no other visible symptom |
| Storage | Per-drive usage (not just an aggregate), so a multi-drive setup is visible at a glance. Reads the host's real mount table from inside the container, deduped by device and filtered to real filesystems only |
| Containers | Live status for every Docker container on the host, with Stop/Restart actions (confirmation required) and a tailable log viewer |
| Update checker | Flags when a running container's image has a newer version available on its registry (Docker Hub only for now), so updates aren't discovered by accident |
| Uptime | Configurable HTTP/TCP/ping checks against services you point it at, with a live status page and 24h/7d uptime % rollups computed from the check history |
| LAN devices | Real ARP scan (not a ping sweep, so it catches devices that block ICMP) via a dedicated network-privileged container, with presence tracking, an OUI vendor lookup, and name resolution via NetBIOS/reverse-DNS/SSDP/mDNS where a device supports one. Manual nicknames and category tagging (trusted/IoT/guest/unknown) cover anything that doesn't auto-resolve |

## Auth

Single shared password, set on first launch and changeable anytime from the dashboard, no container restart needed either way. Sessions use a bearer token, not cookies. There's no lockout or rate limiting on login, by design, under the assumption this stays on your home LAN and isn't exposed beyond it; if you ever enable Umbrel's remote access, treat that as worth revisiting.

## Running outside Umbrel

Lares is a normal Docker app, two containers from one published image; Umbrel packaging (`umbrel-app.yml`, the `app_proxy` service) just adapts it to umbrelOS. To self-host it directly:

```yaml
services:
  lares:
    image: xdeekay/lares:0.1.0
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/backend/data
      - /var/run/docker.sock:/var/run/docker.sock
      - /:/host:ro
      - /proc:/host/proc:ro
      - /etc/localtime:/etc/localtime:ro

  lan-scanner:
    image: xdeekay/lares:0.1.0
    command: ["python", "-m", "backend.collectors.lan"]
    restart: unless-stopped
    network_mode: host
    cap_add:
      - NET_ADMIN
      - NET_RAW
    volumes:
      - ./data:/app/backend/data
      - /etc/localtime:/etc/localtime:ro
```

Open `http://<host>:8000`, set a password, and everything else works from there. `/:/host:ro` and `/proc:/host/proc:ro` are what let the storage panel see the host's real drives instead of just the container's own filesystem, they're optional if you don't care about per-drive storage, but container control and system stats work without them regardless.

`lan-scanner` is what powers LAN device discovery. It asks for real privileges, `network_mode: host` plus `NET_ADMIN`/`NET_RAW`, since an actual ARP scan needs L2 visibility into your physical network that a normal bridge-network container doesn't have. It's optional: the rest of Lares runs fine without it, so skip that service if you'd rather not grant those privileges.

## Data & configuration

All state lives under the single mounted volume, shared by both containers, in one SQLite database (`lares.db`): system/disk/container metrics history, uptime check history, discovered LAN devices and their sighting history, the container action log, and the auth password hash/session tokens. No environment variables are required; the password is set through the first-run screen in the browser, not a config file.

## Status

Early. System monitoring, disk storage, container control, uptime monitoring, single-password auth, and LAN device discovery are built and running in production on the maintainer's own Pi. BLE device discovery, WAN monitoring, and alerting are planned but not yet built.
