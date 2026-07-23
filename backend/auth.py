"""Single shared-password auth: hashing, session tokens, and the FastAPI
dependency that gates the rest of the API.

Stdlib only, deliberately. bcrypt/argon2-cffi would need a C extension built
on the Pi's ARM host, the same class of pain already hit once with psutil.
PBKDF2-SHA256 via hashlib is a fine choice for a single shared password on a
home-lab tool.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException

from backend.db import get_connection

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 600_000  # OWASP 2023 floor for PBKDF2-HMAC-SHA256
SESSION_TTL = timedelta(days=30)
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_str, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations_str))
    return hmac.compare_digest(actual, expected)


def is_password_set() -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM auth_config WHERE id = 1").fetchone()
        return row is not None
    finally:
        conn.close()


def set_password(password: str) -> None:
    encoded = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO auth_config (id, password_hash, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash,
                                           updated_at = excluded.updated_at
            """,
            (encoded, now),
        )
        conn.commit()
    finally:
        conn.close()


def verify_current_password(password: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT password_hash FROM auth_config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return verify_password(password, row["password_hash"])


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(token: str) -> None:
    now = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (hash_token(token), now.isoformat(), (now + SESSION_TTL).isoformat(), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (hash_token(token),))
        conn.commit()
    finally:
        conn.close()


def delete_all_sessions() -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM auth_sessions")
        conn.commit()
    finally:
        conn.close()


def require_auth(authorization: str | None = Header(default=None)) -> str:
    """Validates the bearer token and returns the raw token, so routes that
    need it (e.g. logout, to delete only the caller's own session) can
    receive it via `token: str = Depends(require_auth)`. Routes that only
    need the gate can ignore the return value."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    token_hash = hash_token(token)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT expires_at, last_seen_at FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(row["expires_at"])
        if now >= expires_at:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
            raise HTTPException(status_code=401, detail="Session expired")

        last_seen_at = datetime.fromisoformat(row["last_seen_at"])
        if now - last_seen_at >= SESSION_TOUCH_INTERVAL:
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (now.isoformat(), (now + SESSION_TTL).isoformat(), token_hash),
            )
            conn.commit()
    finally:
        conn.close()

    return token
