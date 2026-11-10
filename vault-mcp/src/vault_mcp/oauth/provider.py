"""FastMCP `OAuthProvider` backed by `tokens.db` (plan D17).

SDK endpoints stay at the host root (`/authorize`, `/token`, `/register`, `/revoke`; T2); the
kit's pages live under `/oauth/`. One verifier serves both credential kinds: `vmcp_` bearer
tokens and `vmcpa_` OAuth access tokens resolve to a `credentials` row on every request.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from fastmcp.server.auth import AccessToken, OAuthProvider
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.routing import Route

from vault_mcp.oauth.grants import (
    ACCESS_TTL,
    OAUTH_SCOPES,
    GrantError,
    OAuthStore,
)
from vault_mcp.store import CredentialStore

VALID_SCOPES = ["vault:read", "vault:write"]
_FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob"}
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}


def redirect_uri_allowed(uri: str) -> bool:
    """OAuth 2.1 §2.3.1 / RFC 8252: https, http only on loopback, or a private-use scheme
    (native apps, e.g. `cursor://`). Never script/data/file schemes."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if not scheme or scheme in _FORBIDDEN_SCHEMES or parsed.fragment:
        return False
    if scheme == "https":
        return bool(parsed.hostname)
    if scheme == "http":
        return parsed.hostname in _LOOPBACK
    return True


class VaultOAuthProvider(OAuthProvider):
    def __init__(self, store: CredentialStore, issuer: str) -> None:
        super().__init__(
            base_url=issuer,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=VALID_SCOPES, default_scopes=VALID_SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
            # No required_scopes (T2): it would narrow what clients ask for. Consent decides
            # the scope and every tool checks the credential's scope.
        )
        self.issuer = issuer.rstrip("/")
        self.store = store
        self.grants = OAuthStore(store)

    # -- clients ------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await asyncio.to_thread(self.grants.get_client, client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_uri_allowed(str(uri)):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect URI not allowed: use https, a loopback http URL, or an app scheme: {uri}",
                )
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "missing client_id")
        ok = await asyncio.to_thread(
            self.grants.add_client,
            client_info.client_id,
            client_info.model_dump(mode="json", exclude_none=True),
        )
        if not ok:
            raise RegistrationError(
                "invalid_client_metadata", "this server has too many registered clients"
            )

    # -- authorize: hand over to the kit's login + consent pages ------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        assert client.client_id is not None  # noqa: S101 - registered clients always have one
        req = await asyncio.to_thread(
            self.grants.create_request, client.client_id, params.model_dump(mode="json")
        )
        return f"{self.issuer}/oauth/login?req={req}"

    # -- codes and tokens ---------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        assert client.client_id is not None  # noqa: S101
        found = await asyncio.to_thread(self.grants.get_code, client.client_id, authorization_code)
        if found is None:
            return None
        params, owner, scope, expires = found
        return AuthorizationCode(
            code=authorization_code, scopes=OAUTH_SCOPES[scope], expires_at=expires.timestamp(),
            client_id=client.client_id, code_challenge=params["code_challenge"], redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params.get("resource"), subject=owner,
        )  # fmt: skip

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        assert client.client_id is not None  # noqa: S101
        name = client.client_name or "client"
        try:
            issued = await asyncio.to_thread(
                self.grants.redeem_code, client.client_id, authorization_code.code, name
            )
        except GrantError as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        return self._token_response(issued.access_token, issued.refresh_token, issued.scope)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        assert client.client_id is not None  # noqa: S101
        found = await asyncio.to_thread(self.grants.refresh_info, client.client_id, refresh_token)
        if found is None:
            return None
        cred, expires = found
        return RefreshToken(
            token=refresh_token, client_id=client.client_id, scopes=OAUTH_SCOPES[cred.scope],
            expires_at=int(expires.timestamp()), subject=cred.owner,
        )  # fmt: skip

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        assert client.client_id is not None  # noqa: S101
        try:
            issued = await asyncio.to_thread(
                self.grants.rotate, client.client_id, refresh_token.token
            )
        except GrantError as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        # The SDK already refused scopes outside the grant; a narrower request is not
        # honoured separately: tools check the grant's scope.
        return self._token_response(issued.access_token, issued.refresh_token, issued.scope)

    @staticmethod
    def _token_response(access: str, refresh: str, scope: str) -> OAuthToken:
        return OAuthToken(
            access_token=access, token_type="Bearer",  # noqa: S106 - the token type name
            expires_in=int(ACCESS_TTL.total_seconds()),
            refresh_token=refresh, scope=" ".join(OAUTH_SCOPES[scope]),
        )  # fmt: skip

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Bearer tokens (`token create`) and OAuth access tokens, both checked live."""
        cred = await asyncio.to_thread(self.store.verify, token)
        if cred is not None:
            return AccessToken(
                token=token, client_id=cred.id, scopes=OAUTH_SCOPES[cred.scope],
                expires_at=int(cred.expires_at.timestamp()) if cred.expires_at else None,
                claims={"credential_id": cred.id},
            )  # fmt: skip
        found = await asyncio.to_thread(self.grants.access, token)
        if found is None:
            return None
        grant, client_id, expires = found
        return AccessToken(
            token=token, client_id=client_id, scopes=OAUTH_SCOPES[grant.scope], expires_at=int(expires.timestamp()),
            subject=grant.owner, claims={"credential_id": grant.id},
        )  # fmt: skip

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """RFC 7009: revoking either token ends the whole grant. Bearer tokens are revoked
        only through `token revoke`."""
        cred_id = await asyncio.to_thread(self.grants.grant_of, token.token)
        if cred_id is not None:
            await asyncio.to_thread(self.grants.revoke_grant, cred_id)

    # -- routes ---------------------------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Advertise `none` (public PKCE clients such as Claude.ai) alongside the secret-based
        token endpoint auth methods; the SDK's registration handler already accepts it."""
        metadata = build_metadata(
            self.issuer_url,  # type: ignore[arg-type]
            None,
            self.client_registration_options or ClientRegistrationOptions(),
            self.revocation_options or RevocationOptions(),
        )
        metadata.token_endpoint_auth_methods_supported = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        handler = cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"])
        return [
            Route(route.path, endpoint=handler, methods=["GET", "OPTIONS"])
            if isinstance(route, Route) and route.path == "/.well-known/oauth-authorization-server"
            else route
            for route in super().get_routes(mcp_path)
        ]
