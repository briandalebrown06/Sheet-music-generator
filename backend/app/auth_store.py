"""Provisioned users, opaque sessions and durable throttles; never plaintext passwords."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1
SCRYPT_MAXMEM = 192 * 1024 * 1024
# An unknown account still performs the same password KDF, avoiding a fast
# account-existence oracle. This is not an account or a usable credential.
DUMMY_HASH = "scrypt$131072$8$1$" + "00" * 16 + "$" + "00" * 32


def normalize_username(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+-]{2,79}", value):
        raise ValueError("Username must be 3–80 ASCII letters, digits, or ._@+- characters.")
    return value


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256 or len(password.encode("utf-8")) > 1024:
        raise ValueError("Password must contain 12–256 characters and at most 1,024 UTF-8 bytes.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                            p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if len(password) > 256 or len(password.encode("utf-8")) > 1024:
        return False
    try:
        algorithm, n, r, p, salt, expected = stored.split("$")
        # Only our reviewed parameters are accepted, including from the DB.
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt_bytes, digest = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(digest) != 32:
            return False
        result = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=SCRYPT_N,
            r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
        return hmac.compare_digest(result, digest)
    except (ValueError, TypeError):
        return False


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
                CREATE TABLE IF NOT EXISTS auth_rate_buckets (
                    key_hash TEXT NOT NULL, window_start INTEGER NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(key_hash, window_start)
                );
            """)

    def create_user(self, username: str, password: str) -> dict:
        username, digest = normalize_username(username), hash_password(password)
        user_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("INSERT INTO auth_users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                         (user_id, username, digest, time.time()))
        return {"id": user_id, "username": username}

    def user(self, username: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> dict | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = "invalid"
        user = self.user(normalized)
        valid = verify_password(password, user["password_hash"] if user else DUMMY_HASH)
        if not user or not valid or user["disabled"]:
            return None
        return {"id": user["id"], "username": user["username"]}

    def set_password(self, username: str, password: str) -> None:
        digest = hash_password(password)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT id FROM auth_users WHERE username=?", (normalize_username(username),)).fetchone()
            if not user:
                raise ValueError("Account not found.")
            conn.execute("UPDATE auth_users SET password_hash=? WHERE id=?", (digest, user["id"]))
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))

    def disable_user(self, username: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT id FROM auth_users WHERE username=?", (normalize_username(username),)).fetchone()
            if not user:
                raise ValueError("Account not found.")
            conn.execute("UPDATE auth_users SET disabled=1 WHERE id=?", (user["id"],))
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))

    def create_session(self, user_id: str, ttl: int, previous_token: str | None = None) -> tuple[str, dict]:
        now, token, csrf = time.time(), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT id,username FROM auth_users WHERE id=? AND disabled=0", (user_id,)).fetchone()
            if not user:
                raise ValueError("Account is unavailable.")
            conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            if previous_token:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(previous_token),))
            # Limit outstanding sessions even when an account repeatedly logs in.
            conn.execute("DELETE FROM auth_sessions WHERE token_hash IN (SELECT token_hash FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 19)", (user_id,))
            conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?)", (token_digest(token),user_id,csrf,now,now+ttl))
        return token, {"user": dict(user), "csrf_token": csrf, "expires_at": now+ttl}

    def session(self, token: str | None) -> dict | None:
        if not token or len(token) > 256:
            return None
        with self.connection() as conn:
            row = conn.execute("""SELECT u.id,u.username,s.csrf_token,s.expires_at FROM auth_sessions s
                JOIN auth_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0""",
                (token_digest(token), time.time())).fetchone()
        if row is None:
            return None
        return {"user": {"id":row["id"], "username":row["username"]},
                "csrf_token":row["csrf_token"], "expires_at":row["expires_at"]}

    def revoke(self, token: str | None):
        if token:
            with self.connection() as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(token),))

    def rate_allowed(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.time()
        window = int(now // window_seconds) * window_seconds
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Keep only bounded recent request metadata, never raw user names/IPs.
            conn.execute("DELETE FROM auth_rate_buckets WHERE window_start<?", (int(now)-86400,))
            key_hash = token_digest(key)
            conn.execute("INSERT INTO auth_rate_buckets VALUES(?,?,1) ON CONFLICT(key_hash,window_start) DO UPDATE SET count=count+1", (key_hash,window))
            count = conn.execute("SELECT count FROM auth_rate_buckets WHERE key_hash=? AND window_start=?", (key_hash,window)).fetchone()[0]
        return count <= limit, max(1, int(window+window_seconds-now)+1)
