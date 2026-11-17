"""Upstream sign-in for the OAuth pages (SPEC §6.3 (2a), plan D18).

Only a verified email is needed: it becomes the grant's owner and must be in
`OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`. Upstream tokens are used once and thrown away.

- `google`, `oidc`: OIDC discovery, code + PKCE, ID token verified (signature via JWKS,
  issuer, audience, expiry, nonce); `email_verified` must be true.
- `microsoft`: OIDC against one tenant (`OAUTH_UPSTREAM_TENANT`, never `common`), since a
  multi-tenant login would accept any tenant's self-asserted email.
- `github`: OAuth app + `/user/emails`; only the primary, verified email counts.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

TIMEOUT = httpx.Timeout(10.0)
_MULTI_TENANT = {"common", "organizations", "consumers"}
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class LoginRefused(Exception):
    """Sign-in did not produce an allowed, verified email. The message is user-facing."""


class Upstream(Protocol):
    name: str

    async def authorize_url(self, state: str, nonce: str, verifier: str) -> str: ...

    async def email(self, code: str, verifier: str, nonce: str) -> str: ...


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def check_domain(email: str, allowed: frozenset[str]) -> str:
    email = email.strip()
    if email.count("@") != 1:
        raise LoginRefused("Your account has no usable email address.")
    domain = email.rsplit("@", 1)[1].lower()
    if domain not in allowed:
        raise LoginRefused(f"Accounts from {domain} can't use this vault. Ask your administrator.")
    return email


@dataclass
class OIDCUpstream:
    name: str
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_domains: frozenset[str]
    require_email_verified: bool = True
    email_claims: tuple[str, ...] = ("email",)
    # Google: only Workspace accounts carry `hd`; a personal Google account registered with a
    # work address does not, and would outlive the person's employment.
    require_hosted_domain: bool = False
    # Microsoft: a tenant configured by domain name discovers its GUID issuer.
    issuer_pattern: str | None = None
    _meta: dict[str, Any] | None = field(default=None, init=False)

    async def _metadata(self) -> dict[str, Any]:
        if self._meta is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                r = await http.get(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
                r.raise_for_status()
                meta = dict(r.json())
            discovered = str(meta.get("issuer", ""))
            if discovered != self.issuer:
                if self.issuer_pattern is None or not re.fullmatch(self.issuer_pattern, discovered):
                    raise LoginRefused(
                        "The identity provider's configuration doesn't match its issuer."
                    )
                self.issuer = discovered
            self._meta = meta
        return self._meta

    async def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        meta = await self._metadata()
        query = {
            "response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        }  # fmt: skip
        return f"{meta['authorization_endpoint']}?{urlencode(query)}"

    async def email(self, code: str, verifier: str, nonce: str) -> str:
        meta = await self._metadata()
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            r = await http.post(
                meta["token_endpoint"],
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                      "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier},
                headers={"Accept": "application/json"},
            )  # fmt: skip
            if r.status_code != 200 or "id_token" not in r.json():
                raise LoginRefused("The identity provider did not complete the sign-in.")
            id_token = r.json()["id_token"]
            jwks = await http.get(meta["jwks_uri"])
            jwks.raise_for_status()
        try:
            token = jwt.decode(id_token, KeySet.import_key_set(jwks.json()))
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": self.issuer},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise LoginRefused(
                "The sign-in token from the identity provider is not valid."
            ) from exc
        claims = token.claims
        if self.require_email_verified and claims.get("email_verified") is not True:
            raise LoginRefused("Your email address is not verified with the identity provider.")
        email = next((str(claims[c]) for c in self.email_claims if claims.get(c)), "")
        email = check_domain(email, self.allowed_domains)
        if (
            self.require_hosted_domain
            and str(claims.get("hd", "")).lower() != email.rsplit("@", 1)[1].lower()
        ):
            raise LoginRefused(
                "Sign in with your organisation's Google Workspace account, not a personal Google account."
            )
        return email


@dataclass
class GitHubUpstream:
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_domains: frozenset[str]
    web: str = "https://github.com"
    api: str = "https://api.github.com"
    name: str = "github"

    async def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        query = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "scope": "user:email", "state": state,
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256", "allow_signup": "false",
        }  # fmt: skip
        return f"{self.web}/login/oauth/authorize?{urlencode(query)}"

    async def email(self, code: str, verifier: str, nonce: str) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            r = await http.post(
                f"{self.web}/login/oauth/access_token",
                data={"client_id": self.client_id, "client_secret": self.client_secret, "code": code,
                      "redirect_uri": self.redirect_uri, "code_verifier": verifier},
                headers={"Accept": "application/json"},
            )  # fmt: skip
            access = r.json().get("access_token") if r.status_code == 200 else None
            if not access:
                raise LoginRefused("GitHub did not complete the sign-in.")
            emails = await http.get(
                f"{self.api}/user/emails",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if emails.status_code != 200:
            raise LoginRefused(
                "GitHub did not share your email addresses (the app needs the user:email scope)."
            )
        primary = next((e for e in emails.json() if e.get("primary")), None)
        if primary is None or primary.get("verified") is not True:
            raise LoginRefused("Your primary GitHub email address is not verified.")
        return check_domain(str(primary["email"]), self.allowed_domains)


def build_upstream(
    provider: str,
    *,
    client_id: str | None,
    client_secret: str | None,
    allowed_domains: str,
    redirect_uri: str,
    issuer: str | None = None,
    tenant: str | None = None,
) -> Upstream | None:
    """None for approval-code sign-in (`""` or `none`). Raises ValueError on bad config, so
    vault-mcp refuses to start rather than letting anyone sign in."""
    provider = (provider or "none").strip().lower()
    if provider == "none":
        return None
    domains = frozenset(
        d.strip().lower().lstrip("@") for d in allowed_domains.split(",") if d.strip()
    )
    if not domains:
        raise ValueError("OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS must list at least one domain")
    if not client_id or not client_secret:
        raise ValueError("OAUTH_UPSTREAM_CLIENT_ID and OAUTH_UPSTREAM_CLIENT_SECRET are required")
    if provider == "google":
        return OIDCUpstream(
            "google", "https://accounts.google.com", client_id, client_secret, redirect_uri, domains,
            require_hosted_domain=True,
        )  # fmt: skip
    if provider == "microsoft":
        if not tenant or tenant.lower() in _MULTI_TENANT or not _TENANT_RE.match(tenant):
            raise ValueError(
                "OAUTH_UPSTREAM_TENANT must be your Microsoft Entra tenant id or domain (not common)"
            )
        return OIDCUpstream(
            "microsoft", f"https://login.microsoftonline.com/{tenant}/v2.0", client_id, client_secret, redirect_uri,
            domains, require_email_verified=False, email_claims=("email", "preferred_username"),
            issuer_pattern=r"https://login\.microsoftonline\.com/[0-9a-f-]{36}/v2\.0",
        )  # fmt: skip
    if provider == "oidc":
        if not issuer or not issuer.startswith("https://"):
            raise ValueError("OAUTH_UPSTREAM_ISSUER must be the provider's https issuer URL")
        return OIDCUpstream("oidc", issuer, client_id, client_secret, redirect_uri, domains)
    if provider == "github":
        return GitHubUpstream(client_id, client_secret, redirect_uri, domains)
    raise ValueError(
        f"OAUTH_UPSTREAM_PROVIDER must be google, microsoft, github, oidc or none, not {provider!r}"
    )
