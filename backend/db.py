"""Shared SQLite access: schema creation and connection helper.

Collectors and the API each open their own connection via get_connection().
WAL mode lets the collector write and the API read concurrently without
locking each other out.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "lares.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


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
        conn.commit()
    finally:
        conn.close()
