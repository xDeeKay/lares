"""Shared SQLite access: schema creation and connection helper.

Collectors and the API each open their own connection via get_connection().
WAL mode lets the collector write and the API read concurrently without
locking each other out.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "lares.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Idempotently add a column to an already-shipped table. SQLite has no
    ADD COLUMN IF NOT EXISTS, so check PRAGMA table_info first. table/column
    are always internal literals (never user input), so the f-string here
    carries no injection risk.

    init_db() runs from five processes at once at container startup (the API
    and all four collectors), so the check-then-add here is a real race: two
    processes can both see the column missing before either commits its own
    ALTER TABLE, and the second one's ALTER then raises "duplicate column
    name". That's the expected outcome of losing the race, not a real
    problem, so it's swallowed; any other OperationalError still surfaces.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_metrics (
                id INTEGER PRIMARY KEY,
                host TEXT,
                timestamp DATETIME,
                cpu_pct REAL,
                mem_used_mb INTEGER,
                mem_total_mb INTEGER,
                temp_c REAL,
                load_1m REAL,
                throttled_flags TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_system_metrics_timestamp "
            "ON system_metrics (timestamp)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disk_info (
                id INTEGER PRIMARY KEY,
                device TEXT,
                mount_point TEXT,
                timestamp DATETIME,
                total_gb REAL,
                used_gb REAL,
                free_gb REAL,
                used_pct REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disk_info_timestamp "
            "ON disk_info (timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disk_info_mount_point "
            "ON disk_info (mount_point)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS containers (
                id INTEGER PRIMARY KEY,
                container_id TEXT UNIQUE,
                name TEXT,
                image TEXT,
                status TEXT,
                update_available BOOLEAN DEFAULT 0,
                last_updated DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS container_metrics (
                id INTEGER PRIMARY KEY,
                container_id TEXT,
                timestamp DATETIME,
                cpu_pct REAL,
                mem_used_mb INTEGER,
                net_rx_bytes INTEGER,
                net_tx_bytes INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_container_metrics_container_id_timestamp "
            "ON container_metrics (container_id, timestamp)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS container_actions (
                id INTEGER PRIMARY KEY,
                container_id TEXT,
                action TEXT,
                timestamp DATETIME,
                success BOOLEAN
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_container_actions_timestamp "
            "ON container_actions (timestamp)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_hash TEXT NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY,
                token_hash TEXT UNIQUE NOT NULL,
                created_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at "
            "ON auth_sessions (expires_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uptime_targets (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                target_type TEXT NOT NULL,
                address TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL
            )
            """
        )
        # Added after the initial Phase 4 ship: per-target overrides for the
        # global poll interval/timeout. NULL means "use the collector's
        # global default." ALTER TABLE (not part of the CREATE above) since
        # uptime_targets already exists on deployed installs.
        _ensure_column(conn, "uptime_targets", "check_interval_seconds", "INTEGER")
        _ensure_column(conn, "uptime_targets", "check_timeout_seconds", "INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uptime_checks (
                id INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL,
                timestamp DATETIME NOT NULL,
                is_up BOOLEAN NOT NULL,
                response_ms INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uptime_checks_target_id_timestamp "
            "ON uptime_checks (target_id, timestamp)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY,
                mac_address TEXT UNIQUE NOT NULL,
                device_type TEXT NOT NULL,
                vendor TEXT,
                hostname TEXT,
                last_ip TEXT,
                category TEXT NOT NULL DEFAULT 'unknown',
                nickname TEXT,
                first_seen DATETIME NOT NULL,
                last_seen DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices (last_seen)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_sightings (
                id INTEGER PRIMARY KEY,
                mac_address TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                ip_address TEXT,
                rssi INTEGER,
                is_present BOOLEAN NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_sightings_mac_timestamp "
            "ON device_sightings (mac_address, timestamp)"
        )
        # Added with Phase 6 (BLE): cheap latest-RSSI lookup without joining
        # device_sightings, same rationale as last_ip above. Only ever
        # populated by the BLE collector; LAN rows just keep this NULL.
        _ensure_column(conn, "devices", "last_rssi", "INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lan_scan_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cidr TEXT,
                effective_cidr TEXT,
                scan_interval_seconds INTEGER NOT NULL DEFAULT 300,
                last_scan_at DATETIME,
                force_scan_requested_at DATETIME,
                updated_at DATETIME NOT NULL
            )
            """
        )
        # A default singleton row so GET /api/lan/settings works out of the
        # box without requiring a first-run setup step, unlike auth_config
        # which deliberately has no default (no password should exist until
        # the user sets one).
        conn.execute(
            """
            INSERT OR IGNORE INTO lan_scan_settings (id, scan_interval_seconds, updated_at)
            VALUES (1, 300, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_scan_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                flush_interval_seconds INTEGER NOT NULL DEFAULT 30,
                last_flush_at DATETIME,
                force_flush_requested_at DATETIME,
                updated_at DATETIME NOT NULL
            )
            """
        )
        # No cidr-equivalent column here: unlike LAN scanning, BLE has no
        # scan-target concept, just how often the collector's continuously
        # buffered advertisements get flushed to disk. Same default-row
        # rationale as lan_scan_settings above (GET /api/ble/settings works
        # out of the box, no first-run setup step).
        conn.execute(
            """
            INSERT OR IGNORE INTO ble_scan_settings (id, flush_interval_seconds, updated_at)
            VALUES (1, 30, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()
