"""OAuth state in `tokens.db` (plan D16/D17): clients, pending authorization requests,
authorization codes, approval codes, and grants.

A grant is a `credentials` row (`kind = 'oauth'`), so `token list` shows it and `token revoke`
ends it: its access and refresh tokens stop working on the next request. Every secret
(request ids, browser nonces, codes, tokens) is stored as sha256 only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from vault_core.audit import new_id
from vault_mcp.store import LAST_USED_RESOLUTION, Credential, CredentialStore, Scope

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
REQUEST_TTL = timedelta(minutes=15)
CODE_TTL = timedelta(minutes=5)
MAX_LOGIN_ATTEMPTS = 5  # wrong approval codes per authorization request
GLOBAL_FAILURES, GLOBAL_WINDOW = 30, timedelta(minutes=10)
MAX_CLIENTS = 1000
STALE_CLIENT = timedelta(days=7)

ACCESS_PREFIX, REFRESH_PREFIX, CODE_PREFIX = "vmcpa_", "vmcpr_", "vmcpc_"
_OPAQUE_RE = re.compile(r"^vmcp[arc]_[A-Za-z0-9_-]{43}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

OAUTH_SCOPES = {"read": ["vault:read"], "write": ["vault:read", "vault:write"]}


def scope_from_oauth(scopes: list[str] | None) -> Scope:
    return "write" if scopes is None or "vault:write" in scopes else "read"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret(prefix: str) -> str:
    return prefix + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def is_opaque(token: str) -> bool:
    return bool(_OPAQUE_RE.match(token))


def normalize_approval_code(text: str) -> str:
    """Codes are shown as XXXXX-XXXXX; accept any case, spacing, dashes, and O/I/L lookalikes."""
    cleaned = re.sub(r"[\s-]", "", text).upper().translate(str.maketrans("OIL", "011"))
    return cleaned


@dataclass(frozen=True)
class PendingRequest:
    client_id: str
    params: dict[str, Any]
    browser_hash: str | None
    csrf_hash: str | None
    owner: str | None
    max_scope: Scope | None
    attempts: int


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    scope: Scope
    credential: Credential


class GrantError(Exception):
    """A token-endpoint failure (`invalid_grant`); the message is safe to return."""


class OAuthStore:
    def __init__(self, store: CredentialStore) -> None:
        self.store = store

    # -- clients ----------------------------------------------------------------------------

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return json.loads(row["info"]) if row else None

    def add_client(self, client_id: str, info: dict[str, Any]) -> bool:
        """Register a client; prunes stale unused ones first. False when the cap is reached."""
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM oauth_clients WHERE created_at < ? AND client_id NOT IN"
                " (SELECT client_id FROM token_hashes WHERE client_id IS NOT NULL)"
                " AND client_id NOT IN (SELECT client_id FROM auth_codes)"
                " AND client_id NOT IN (SELECT client_id FROM auth_requests)",
                (_iso(now - STALE_CLIENT),),
            )
            count = conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
            if count >= MAX_CLIENTS:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO oauth_clients (client_id, info, created_at) VALUES (?, ?, ?)",
                (client_id, json.dumps(info), _iso(now)),
            )
            conn.execute("COMMIT")
        return True

    # -- authorization requests (between /authorize and the consent page) --------------------

    def create_request(self, client_id: str, params: dict[str, Any]) -> str:
        req = secrets.token_urlsafe(32)
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("DELETE FROM auth_requests WHERE expires_at < ?", (_iso(now),))
            conn.execute(
                "INSERT INTO auth_requests (hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (_hash(req), client_id, json.dumps(params), _iso(now + REQUEST_TTL)),
            )
        return req

    def get_request(self, req: str) -> PendingRequest | None:
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT * FROM auth_requests WHERE hash = ? AND expires_at > ?",
                (_hash(req), _iso(self.store.now())),
            ).fetchone()
        if row is None:
            return None
        return PendingRequest(
            client_id=row["client_id"], params=json.loads(row["params"]), browser_hash=row["browser_hash"],
            csrf_hash=row["csrf_hash"], owner=row["owner"], max_scope=row["max_scope"], attempts=row["attempts"],
        )  # fmt: skip

    def bind_browser(self, req: str) -> tuple[str, str]:
        """New browser nonce (cookie) and CSRF token (form field) for this request."""
        nonce, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.db() as conn:
            conn.execute(
                "UPDATE auth_requests SET browser_hash = ?, csrf_hash = ? WHERE hash = ?",
                (_hash(nonce), _hash(csrf), _hash(req)),
            )
        return nonce, csrf

    @staticmethod
    def cookie_matches(pending: PendingRequest, nonce: str | None) -> bool:
        """The browser that opened this request (its HttpOnly cookie)."""
        return (
            pending.browser_hash is not None and nonce is not None
            and secrets.compare_digest(pending.browser_hash, _hash(nonce))
        )  # fmt: skip

    @classmethod
    def browser_matches(cls, pending: PendingRequest, nonce: str | None, csrf: str | None) -> bool:
        """Same browser, and the form it was last given (one-time CSRF token)."""
        return (
            cls.cookie_matches(pending, nonce) and pending.csrf_hash is not None and csrf is not None
            and secrets.compare_digest(pending.csrf_hash, _hash(csrf))
        )  # fmt: skip

    def identify(self, req: str, owner: str, max_scope: Scope) -> None:
        """Signed in upstream: record who, and forget the one-time upstream values."""
        with self.store.db() as conn:
            conn.execute(
                "UPDATE auth_requests SET owner = ?, max_scope = ?, upstream_verifier = NULL, upstream_nonce = NULL"
                " WHERE hash = ?",
                (owner, max_scope, _hash(req)),
            )

    def start_upstream(self, req: str) -> tuple[str, str]:
        """PKCE verifier and OIDC nonce for the identity-provider round trip."""
        verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        with self.store.db() as conn:
            conn.execute(
                "UPDATE auth_requests SET upstream_verifier = ?, upstream_nonce = ? WHERE hash = ?",
                (verifier, nonce, _hash(req)),
            )
        return verifier, nonce

    def upstream_values(self, req: str) -> tuple[str, str] | None:
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT upstream_verifier, upstream_nonce FROM auth_requests WHERE hash = ? AND expires_at > ?",
                (_hash(req), _iso(self.store.now())),
            ).fetchone()
        if row is None or row["upstream_verifier"] is None:
            return None
        return row["upstream_verifier"], row["upstream_nonce"]

    def drop_request(self, req: str) -> None:
        with self.store.db() as conn:
            conn.execute("DELETE FROM auth_requests WHERE hash = ?", (_hash(req),))

    # -- approval codes (OAUTH_UPSTREAM_PROVIDER=none) ----------------------------------------

    def create_approval_code(self, owner: str, scope: Scope, ttl: timedelta) -> str:
        raw = "".join(secrets.choice(_CROCKFORD) for _ in range(10))
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute(
                "INSERT INTO approval_codes (hash, owner, scope, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (_hash(raw), owner, scope, _iso(now), _iso(now + ttl)),
            )
        return f"{raw[:5]}-{raw[5:]}"

    def login_with_approval_code(self, req: str, code: str) -> str | None:
        """Consume a code for this request. Returns None on success, else a user-facing reason.
        Wrong codes count against the request (5 tries) and globally (30 per 10 minutes)."""
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            failures = conn.execute(
                "SELECT COUNT(*) FROM login_failures WHERE ts > ?", (_iso(now - GLOBAL_WINDOW),)
            ).fetchone()[0]
            if failures >= GLOBAL_FAILURES:
                conn.execute("ROLLBACK")
                return (
                    "Too many failed sign-in attempts on this server. Try again in a few minutes."
                )
            row = conn.execute(
                "SELECT owner, scope FROM approval_codes WHERE hash = ? AND used_at IS NULL AND expires_at > ?",
                (_hash(normalize_approval_code(code)), _iso(now)),
            ).fetchone()
            if row is None:
                conn.execute(
                    "DELETE FROM login_failures WHERE ts <= ?", (_iso(now - GLOBAL_WINDOW),)
                )
                conn.execute("INSERT INTO login_failures (ts) VALUES (?)", (_iso(now),))
                attempts = conn.execute(
                    "UPDATE auth_requests SET attempts = attempts + 1 WHERE hash = ? RETURNING attempts",
                    (_hash(req),),
                ).fetchone()
                if attempts is not None and attempts[0] >= MAX_LOGIN_ATTEMPTS:
                    conn.execute("DELETE FROM auth_requests WHERE hash = ?", (_hash(req),))
                    conn.execute("COMMIT")
                    return "Too many wrong codes. Start the connection again from your app."
                conn.execute("COMMIT")
                return "That code is not valid, already used, or expired."
            conn.execute(
                "UPDATE approval_codes SET used_at = ? WHERE hash = ?",
                (_iso(now), _hash(normalize_approval_code(code))),
            )
            conn.execute(
                "UPDATE auth_requests SET owner = ?, max_scope = ? WHERE hash = ?",
                (row["owner"], row["scope"], _hash(req)),
            )
            conn.execute("COMMIT")
        return None

    # -- authorization codes -----------------------------------------------------------------

    def create_code(self, client_id: str, params: dict[str, Any], owner: str, scope: Scope) -> str:
        code = _secret(CODE_PREFIX)
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (_iso(now),))
            conn.execute(
                "INSERT INTO auth_codes (hash, client_id, params, owner, scope, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_hash(code), client_id, json.dumps(params), owner, scope, _iso(now + CODE_TTL)),
            )
        return code

    def get_code(
        self, client_id: str, code: str
    ) -> tuple[dict[str, Any], str, Scope, datetime] | None:
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT * FROM auth_codes WHERE hash = ? AND client_id = ? AND expires_at > ?",
                (_hash(code), client_id, _iso(self.store.now())),
            ).fetchone()
        if row is None:
            return None
        return (
            json.loads(row["params"]),
            row["owner"],
            row["scope"],
            datetime.fromisoformat(row["expires_at"]),
        )

    def redeem_code(self, client_id: str, code: str, client_name: str) -> IssuedTokens:
        """Single use: the code is deleted in the same transaction that creates the grant."""
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "DELETE FROM auth_codes WHERE hash = ? AND client_id = ? AND expires_at > ? RETURNING owner, scope",
                (_hash(code), client_id, _iso(now)),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise GrantError("authorization code is invalid, expired, or already used")
            cred_id = new_id()
            slug = _SLUG_RE.sub("-", client_name).strip("-")[:40] or "client"
            conn.execute(
                "INSERT INTO credentials (id, kind, name, owner, scope, folders, token_prefix, created_at, expires_at)"
                " VALUES (?, 'oauth', ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    cred_id,
                    f"oauth:{slug}-{cred_id[-6:].lower()}",
                    row["owner"],
                    row["scope"],
                    _iso(now),
                    _iso(now + REFRESH_TTL),
                ),
            )
            access, refresh = self._issue(conn, cred_id, client_id, now)
            conn.execute("COMMIT")
        return IssuedTokens(access, refresh, row["scope"], self._cred(cred_id))

    @staticmethod
    def _issue(conn: Any, cred_id: str, client_id: str, now: datetime) -> tuple[str, str]:
        access, refresh = _secret(ACCESS_PREFIX), _secret(REFRESH_PREFIX)
        conn.execute(
            "INSERT INTO token_hashes (hash, credential_id, type, expires_at, client_id) VALUES (?, ?, 'access', ?, ?)",
            (_hash(access), cred_id, _iso(now + ACCESS_TTL), client_id),
        )
        conn.execute(
            "INSERT INTO token_hashes (hash, credential_id, type, expires_at, client_id) VALUES (?, ?, 'refresh', ?, ?)",
            (_hash(refresh), cred_id, _iso(now + REFRESH_TTL), client_id),
        )
        return access, refresh

    def _cred(self, cred_id: str) -> Credential:
        cred = self.store.get(cred_id)
        if cred is None:
            raise RuntimeError(f"credential {cred_id} vanished")
        return cred

    # -- tokens --------------------------------------------------------------------------------

    def access(self, token: str) -> tuple[Credential, str, datetime] | None:
        """(credential, client_id, expiry) for a live access token of an active grant."""
        if not token.startswith(ACCESS_PREFIX) or not is_opaque(token):
            return None
        now = self.store.now()
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT credential_id, client_id, expires_at FROM token_hashes WHERE hash = ? AND type = 'access'",
                (_hash(token),),
            ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= now:
            return None
        cred = self.store.get(row["credential_id"])
        if cred is None or cred.status(now) != "active":
            return None
        if cred.last_used_at is None or now - cred.last_used_at >= LAST_USED_RESOLUTION:
            with self.store.db() as conn:
                conn.execute(
                    "UPDATE credentials SET last_used_at = ? WHERE id = ?", (_iso(now), cred.id)
                )
        return cred, row["client_id"], datetime.fromisoformat(row["expires_at"])

    def refresh_info(self, client_id: str, token: str) -> tuple[Credential, datetime] | None:
        """A usable refresh token. Presenting one that was already rotated revokes the whole
        grant (reuse detection, OAuth 2.1 §4.3.1): someone else may hold a copy."""
        if not token.startswith(REFRESH_PREFIX) or not is_opaque(token):
            return None
        now = self.store.now()
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT credential_id, client_id, expires_at, used_at FROM token_hashes WHERE hash = ? AND type = 'refresh'",
                (_hash(token),),
            ).fetchone()
        if row is None or row["client_id"] != client_id:
            return None
        if row["used_at"] is not None:
            self.revoke_grant(row["credential_id"])
            return None
        cred = self.store.get(row["credential_id"])
        if (
            cred is None
            or cred.status(now) != "active"
            or datetime.fromisoformat(row["expires_at"]) <= now
        ):
            return None
        return cred, datetime.fromisoformat(row["expires_at"])

    def rotate(self, client_id: str, token: str) -> IssuedTokens:
        now = self.store.now()
        with self.store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "UPDATE token_hashes SET used_at = ? WHERE hash = ? AND type = 'refresh' AND client_id = ?"
                " AND used_at IS NULL AND expires_at > ? RETURNING credential_id",
                (_iso(now), _hash(token), client_id, _iso(now)),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise GrantError("refresh token is invalid, expired, or already used")
            cred_id = row["credential_id"]
            active = conn.execute(
                "SELECT 1 FROM credentials WHERE id = ? AND revoked_at IS NULL", (cred_id,)
            ).fetchone()
            if active is None:
                conn.execute("ROLLBACK")
                raise GrantError("the grant was revoked")
            access, refresh = self._issue(conn, cred_id, client_id, now)
            conn.execute(
                "UPDATE credentials SET expires_at = ? WHERE id = ?",
                (_iso(now + REFRESH_TTL), cred_id),
            )
            conn.execute("COMMIT")
        cred = self._cred(cred_id)
        return IssuedTokens(access, refresh, cred.scope, cred)

    def revoke_grant(self, cred_id: str) -> None:
        with self.store.db() as conn:
            conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND kind = 'oauth' AND revoked_at IS NULL",
                (_iso(self.store.now()), cred_id),
            )

    def grant_of(self, token: str) -> str | None:
        with self.store.db() as conn:
            row = conn.execute(
                "SELECT credential_id FROM token_hashes WHERE hash = ? AND type IN ('access', 'refresh')",
                (_hash(token),),
            ).fetchone()
        return row["credential_id"] if row else None
