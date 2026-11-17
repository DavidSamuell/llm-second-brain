"""FastMCP `OAuthProvider` backed by `tokens.db` (plan D17).

SDK endpoints stay at the host root (`/authorize`, `/token`, `/register`, `/revoke`; T2); the
kit's pages live under `/oauth/`. One verifier serves both credential kinds: `vmcp_` bearer
tokens and `vmcpa_` OAuth access tokens resolve to a `credentials` row on every request.
"""

from __future__ import annotations

import asyncio
import base64
import html
import secrets
from urllib.parse import parse_qs, urlparse

from fastmcp.server.auth import AccessToken, OAuthProvider
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import _body_limited, _cors, build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, request_response

from vault_mcp.oauth.grants import (
    ACCESS_TTL,
    OAUTH_SCOPES,
    GrantError,
    OAuthStore,
    SecurityEvent,
)
from vault_mcp.store import CredentialStore

VALID_SCOPES = ["vault:read", "vault:write"]
# Script/data schemes, and OS protocol handlers that launch local programs (ms-msdt:, search-ms:…).
_FORBIDDEN_SCHEMES = {
    "javascript",
    "data",
    "file",
    "vbscript",
    "about",
    "blob",
    "search",
    "search-ms",
    "shell",
    "help",
    "jar",
}
MAX_STATE, MAX_RESOURCE = 512, 512
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}


def redirect_uri_allowed(uri: str) -> bool:
    """OAuth 2.1 §2.3.1 / RFC 8252: https, http only on loopback, or a private-use scheme
    (native apps, e.g. `cursor://`). Never script/data/file schemes."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if not scheme or scheme in _FORBIDDEN_SCHEMES or scheme.startswith("ms-") or parsed.fragment:
        return False
    if (
        "@" in parsed.netloc
    ):  # `https://claude.ai@evil.example/` would read as claude.ai on the consent page
        return False
    if scheme == "https":
        return bool(parsed.hostname)
    if scheme == "http":
        return parsed.hostname in _LOOPBACK
    return True


class VaultOAuthProvider(OAuthProvider):
    def __init__(
        self, store: CredentialStore, issuer: str, on_event: SecurityEvent | None = None
    ) -> None:
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
        self.grants = OAuthStore(store, on_event)
        self.on_event: SecurityEvent = self.grants.on_event

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
        if len(params.code_challenge) != 43:  # S256 of a verifier: always 43 base64url characters
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 challenge")
        if len(params.state or "") > MAX_STATE or len(params.resource or "") > MAX_RESOURCE:
            raise AuthorizeError("invalid_request", "state or resource is too long")
        req = await asyncio.to_thread(
            self.grants.create_request, client.client_id, params.model_dump(mode="json")
        )
        if req is None:
            raise AuthorizeError(
                "temporarily_unavailable",
                "too many sign-ins are in progress; try again in a few minutes",
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
        """Three changes to the SDK routes:
        - metadata advertises `none` (public PKCE clients such as Claude.ai);
        - `/authorize` only ever redirects to the kit's sign-in page: the SDK would send the
          browser straight to a self-registered client's URI on errors (an open redirect on
          the vault's hostname), so those become an error page;
        - `/revoke` accepts public clients without `client_secret` (the SDK requires the field)."""
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
        replaced = {
            "/.well-known/oauth-authorization-server": Route(
                "/.well-known/oauth-authorization-server",
                endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
            "/authorize": Route(
                "/authorize", endpoint=_body_limited(request_response(self._authorize_guarded)), methods=["GET", "POST"]
            ),
            "/revoke": Route(
                "/revoke", endpoint=_cors(_body_limited(request_response(self._revoke)), ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            ),
        }  # fmt: skip
        return [
            replaced.get(route.path, route) if isinstance(route, Route) else route
            for route in super().get_routes(mcp_path)
        ]

    async def _authorize_guarded(self, request: Request) -> Response:
        response = await AuthorizationHandler(self).handle(request)
        location = response.headers.get("location", "")
        if 300 <= response.status_code < 400 and not location.startswith(
            f"{self.issuer}/oauth/login?req="
        ):
            query = parse_qs(urlparse(location).query)
            reason = (
                query.get("error_description")
                or query.get("error")
                or ["the request was not valid"]
            )[0]
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>Can't continue</title>"
                f"<h1>Can't continue</h1><p>The app's sign-in request was refused: {html.escape(reason)}</p>",
                status_code=400, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
            )  # fmt: skip
        return response

    async def _revoke(self, request: Request) -> Response:
        """RFC 7009. Public clients send only client_id; confidential ones must authenticate.
        Unknown tokens, and tokens of other clients, are ignored (always 200, per the RFC)."""
        form = await request.form()
        token, client_id = str(form.get("token") or ""), str(form.get("client_id") or "")
        secret = str(form.get("client_secret") or "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                client_id, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
        client = await self.get_client(client_id) if client_id else None
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        public = client.token_endpoint_auth_method == "none"  # noqa: S105 - an auth method name
        if not public and not (
            client.client_secret and secrets.compare_digest(client.client_secret, secret)
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if token:
            owner = await asyncio.to_thread(self.grants.grant_client, token)
            if owner is not None and owner[1] == client_id:
                await asyncio.to_thread(self.grants.revoke_grant, owner[0])
                self.on_event("oauth_revoke", {"credential_id": owner[0], "client_id": client_id})
        return Response(status_code=200, headers={"Cache-Control": "no-store"})
