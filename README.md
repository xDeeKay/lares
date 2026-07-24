# Lares for Umbrel

A custom, from-scratch monitoring dashboard for a home lab running on Umbrel. Tracks system resources, per-drive storage, and Docker container status, with stop/restart control and image update checks built in, all from one dashboard instead of stitching together separate tools.

Named after the Lares of Roman household religion: guardian spirits believed to watch over a home and everyone in it.

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

## Auth

Single shared password, set on first launch and changeable anytime from the dashboard, no container restart needed either way. Sessions use a bearer token, not cookies. There's no lockout or rate limiting on login, by design, under the assumption this stays on your home LAN and isn't exposed beyond it; if you ever enable Umbrel's remote access, treat that as worth revisiting.

## Running outside Umbrel

Lares is a normal single-container Docker app; Umbrel packaging (`umbrel-app.yml`, the `app_proxy` service) just adapts it to umbrelOS. To self-host it directly:

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
```

Open `http://<host>:8000`, set a password, and everything else works from there. `/:/host:ro` and `/proc:/host/proc:ro` are what let the storage panel see the host's real drives instead of just the container's own filesystem, they're optional if you don't care about per-drive storage, but container control and system stats work without them regardless.

## Data & configuration

All state lives under the single mounted volume, in one SQLite database (`lares.db`): system/disk/container metrics history, the container action log, and the auth password hash/session tokens. No environment variables are required; the password is set through the first-run screen in the browser, not a config file.

## Status

Early. System monitoring, disk storage, container control, and single-password auth are built and running in production on the maintainer's own Pi. Uptime monitoring, LAN/BLE device discovery, WAN monitoring, and alerting are planned but not yet built, see the project's own build-phase notes for the current roadmap.
