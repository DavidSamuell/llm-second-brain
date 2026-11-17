"""Credential store in `/data/tokens.db` (SPEC §6.3, plan D16).

One store for bearer tokens now and OAuth grants later (T24). Only sha256(token) is stored;
the plaintext is returned once at creation. A token is `vmcp_` + 32 random bytes
(base64url, 43 chars); its first 8 random characters are kept as a display prefix.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from vault_core.audit import new_id
from vault_core.policy import validate_prefixes

Scope = Literal["read", "write"]
Status = Literal["active", "expired", "revoked"]
TOKEN_PREFIX = "vmcp_"  # noqa: S105 - a public prefix, not a secret
DEFAULT_EXPIRY = "90d"
LAST_USED_RESOLUTION = timedelta(minutes=1)

_TOKEN_RE = re.compile(r"^vmcp_[A-Za-z0-9_-]{43}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$")
OWNER_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_EXPIRY_RE = re.compile(r"^([1-9][0-9]*)([dhm])$")
_UNITS = {"d": "days", "h": "hours", "m": "minutes"}

SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE credentials (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('bearer', 'oauth')),
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    folders TEXT,
    token_prefix TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE UNIQUE INDEX credentials_active_name ON credentials(name) WHERE revoked_at IS NULL;
CREATE TABLE token_hashes (
    hash TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL REFERENCES credentials(id),
    type TEXT NOT NULL CHECK (type IN ('bearer', 'access', 'refresh')),
    expires_at TEXT
);
"""
# v2 (T24): OAuth 2.1 grants share the credentials/token_hashes tables (D16/D17).
_MIGRATE_V2 = """
ALTER TABLE token_hashes ADD COLUMN client_id TEXT;
ALTER TABLE token_hashes ADD COLUMN used_at TEXT;
CREATE INDEX token_hashes_credential ON token_hashes(credential_id);
CREATE TABLE oauth_clients (
    client_id TEXT PRIMARY KEY,
    info TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE auth_requests (
    hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    browser_hash TEXT,
    csrf_hash TEXT,
    owner TEXT,
    max_scope TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    upstream_verifier TEXT,
    upstream_nonce TEXT,
    expires_at TEXT NOT NULL
);
CREATE TABLE auth_codes (
    hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE approval_codes (
    hash TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE login_failures (ts TEXT NOT NULL);
"""


class CredentialError(ValueError):
    """Invalid input or an operation that the store refuses; the message is user-facing."""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_expiry(text: str, now: datetime) -> datetime | None:
    """`never` → no expiry; `<n>d|h|m` → now + n units. Anything else is rejected."""
    if text == "never":
        return None
    match = _EXPIRY_RE.match(text)
    if not match:
        raise CredentialError(f"invalid --expires {text!r}: use <n>d, <n>h, <n>m, or never")
    return now + timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


@dataclass(frozen=True)
class Credential:
    id: str
    kind: Literal["bearer", "oauth"]
    name: str
    owner: str
    scope: Scope
    folders: list[str] | None
    token_prefix: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None

    def allows(self, needed: Scope) -> bool:
        return needed == "read" or self.scope == "write"

    def status(self, now: datetime) -> Status:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and now >= self.expires_at:
            return "expired"
        return "active"

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> Credential:
        return cls(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            owner=row["owner"],
            scope=row["scope"],
            folders=json.loads(row["folders"]) if row["folders"] else None,
            token_prefix=row["token_prefix"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=_dt(row["expires_at"]),
            revoked_at=_dt(row["revoked_at"]),
            last_used_at=_dt(row["last_used_at"]),
        )


class CredentialStore:
    def __init__(
        self, path: Path, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.path = path
        self.now = now
        self._ensure_schema()

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        old_umask = os.umask(0o077)  # WAL/SHM side files are created private too
        try:
            with self.db() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    conn.executescript(
                        f"BEGIN; {_SCHEMA} {_MIGRATE_V2} PRAGMA user_version=2; COMMIT;"
                    )
                elif version == 1:
                    conn.executescript(f"BEGIN; {_MIGRATE_V2} PRAGMA user_version=2; COMMIT;")
                elif version > SCHEMA_VERSION:
                    raise CredentialError(
                        f"{self.path}: schema v{version} is newer than this vault-mcp"
                    )
        finally:
            os.umask(old_umask)

    def create_bearer(
        self,
        *,
        name: str,
        owner: str,
        scope: str,
        folders: list[str] | None = None,
        expires: str = DEFAULT_EXPIRY,
    ) -> tuple[str, Credential]:
        if not _NAME_RE.match(name):
            raise CredentialError(f"invalid name {name!r}: letters, digits, . _ : @ - (max 64)")
        if not OWNER_RE.match(owner):
            raise CredentialError(f"invalid owner {owner!r}: expected an email address")
        if scope not in ("read", "write"):
            raise CredentialError(f"invalid scope {scope!r}: expected read or write")
        if folders is not None:
            try:
                validate_prefixes(folders)
            except ValueError as exc:
                raise CredentialError(f"invalid folder: {exc}") from None
        now = self.now()
        expires_at = parse_expiry(expires, now)
        token = (
            TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        )
        cred_id = new_id()
        with self.db() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO credentials (id, kind, name, owner, scope, folders, token_prefix,"
                    " created_at, expires_at) VALUES (?, 'bearer', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cred_id,
                        name,
                        owner,
                        scope,
                        json.dumps(folders) if folders is not None else None,
                        token[5:13],
                        _iso(now),
                        _iso(expires_at),
                    ),
                )
                conn.execute(
                    "INSERT INTO token_hashes (hash, credential_id, type, expires_at) VALUES (?, ?, 'bearer', ?)",
                    (_hash(token), cred_id, _iso(expires_at)),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise CredentialError(
                    f"an active credential named {name!r} already exists"
                ) from None
        return token, self._require(cred_id)

    def get(self, cred_id: str) -> Credential | None:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return Credential._from_row(row) if row else None

    def _require(self, cred_id: str) -> Credential:
        cred = self.get(cred_id)
        if cred is None:
            raise RuntimeError(f"credential {cred_id} vanished during the operation")
        return cred

    def verify(self, token: str) -> Credential | None:
        """The active credential for a presented token, or None. Records last use."""
        if not _TOKEN_RE.match(token):
            return None
        now = self.now()
        with self.db() as conn:
            row = conn.execute(
                "SELECT c.*, t.expires_at AS token_expires_at FROM token_hashes t"
                " JOIN credentials c ON c.id = t.credential_id"
                " WHERE t.hash = ? AND t.type = 'bearer'",
                (_hash(token),),
            ).fetchone()
            if row is None:
                return None
            cred = Credential._from_row(row)
            token_expires = _dt(row["token_expires_at"])
            if cred.status(now) != "active" or (token_expires is not None and now >= token_expires):
                return None
            if cred.last_used_at is None or now - cred.last_used_at >= LAST_USED_RESOLUTION:
                conn.execute(
                    "UPDATE credentials SET last_used_at = ? WHERE id = ?", (_iso(now), cred.id)
                )
        return cred

    def revoke(self, name_or_id: str) -> Credential:
        now = self.now()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM credentials WHERE revoked_at IS NULL AND (id = ? OR name = ?)",
                (name_or_id, name_or_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise CredentialError(f"no active credential named or with id {name_or_id!r}")
            conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ?", (_iso(now), row["id"])
            )
            conn.execute("COMMIT")
        return self._require(row["id"])

    def list_credentials(self) -> list[Credential]:
        with self.db() as conn:
            rows = conn.execute("SELECT * FROM credentials ORDER BY created_at, id").fetchall()
        return [Credential._from_row(r) for r in rows]
