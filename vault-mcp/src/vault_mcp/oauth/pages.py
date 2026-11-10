"""The kit's OAuth pages under `/oauth/` (plan D17): sign-in and consent.

Server-rendered HTML, no JavaScript. Each authorization request is bound to the browser that
opened it (an HttpOnly cookie) and every form carries a one-time CSRF token, so a link to
someone else's request can't be completed in another browser. Pages can't be framed.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from vault_core.audit import AuditLog
from vault_mcp.oauth.grants import PendingRequest, scope_from_oauth
from vault_mcp.oauth.provider import VaultOAuthProvider
from vault_mcp.oauth.upstream import LoginRefused, Upstream

COOKIE = "vmcp_oauth"
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # the request id is in the URL
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5}"
    "input[type=text]{font-size:1.2rem;letter-spacing:.1em;padding:.4rem;width:12rem}"
    "button{font-size:1rem;padding:.4rem 1rem;margin-right:.5rem}.err{color:#b00020}"
    "dl{display:grid;grid-template-columns:auto 1fr;gap:.25rem 1rem}dt{font-weight:600}"
)

# A login backend turns a GET of /oauth/login into either a page or a redirect to an
# identity provider. T24 ships approval codes; T25 adds upstream OIDC.
LoginStart = Callable[[Request, str, PendingRequest], Awaitable[Response]]


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def error_page(message: str, status: int = 400) -> HTMLResponse:
    return page("Can't continue", f"<p class=err>{html.escape(message)}</p>", status)


class OAuthPages:
    def __init__(
        self, provider: VaultOAuthProvider, audit: AuditLog, upstream: Upstream | None = None
    ) -> None:
        self.provider = provider
        self.grants = provider.grants
        self.audit = audit
        self.upstream = upstream
        self.secure_cookie = provider.issuer.startswith("https://")
        self.login_start: LoginStart = (
            self._upstream_start if upstream else self._approval_form_start
        )

    # -- helpers --------------------------------------------------------------------------------

    def _with_cookie(self, response: Response, nonce: str) -> Response:
        response.set_cookie(
            COOKIE,
            nonce,
            max_age=900,
            path="/oauth",
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
        )
        return response

    async def _pending(self, req: str | None) -> PendingRequest | None:
        return await asyncio.to_thread(self.grants.get_request, req) if req else None

    def _same_browser(self, request: Request, pending: PendingRequest, csrf: str | None) -> bool:
        return self.grants.browser_matches(pending, request.cookies.get(COOKIE), csrf)

    # -- approval-code sign-in -------------------------------------------------------------------

    def _approval_form(self, req: str, error: str | None = None) -> Response:
        nonce, csrf = self.grants.bind_browser(req)
        err = f"<p class=err>{html.escape(error)}</p>" if error else ""
        response = page(
            "Sign in to the docs vault",
            f"{err}<p>Enter the one-time approval code your administrator gave you.</p>"
            f"<form method=post action='/oauth/login'>"
            f"<input type=hidden name=req value='{html.escape(req, quote=True)}'>"
            f"<input type=hidden name=csrf value='{html.escape(csrf, quote=True)}'>"
            f"<p><input type=text name=code autocomplete=off autocapitalize=characters required "
            f"placeholder='XXXXX-XXXXX' aria-label='Approval code'></p>"
            f"<p><button type=submit>Continue</button></p></form>",
        )
        return self._with_cookie(response, nonce)

    async def _approval_form_start(
        self, request: Request, req: str, pending: PendingRequest
    ) -> Response:
        return self._approval_form(req)

    # -- upstream identity provider (D18) ----------------------------------------------------

    async def _upstream_start(
        self, request: Request, req: str, pending: PendingRequest
    ) -> Response:
        assert self.upstream is not None  # noqa: S101 - only installed with an upstream
        nonce_cookie, _ = await asyncio.to_thread(self.grants.bind_browser, req)
        verifier, oidc_nonce = await asyncio.to_thread(self.grants.start_upstream, req)
        try:
            target = await self.upstream.authorize_url(req, oidc_nonce, verifier)
        except (httpx.HTTPError, LoginRefused):
            return error_page(
                "The sign-in service is not reachable right now. Try again in a minute.", 502
            )
        return self._with_cookie(
            RedirectResponse(target, status_code=302, headers=_HEADERS), nonce_cookie
        )

    async def callback(self, request: Request) -> Response:
        """The identity provider sends the browser back here; `state` is the request id."""
        req = request.query_params.get("state") or ""
        pending = await self._pending(req)
        if self.upstream is None or pending is None:
            return error_page(
                "This sign-in has expired or is not valid. Start the connection again from your app."
            )
        if not self.grants.cookie_matches(pending, request.cookies.get(COOKIE)):
            return error_page(
                "This sign-in was started in another browser. Start again from your app."
            )
        values = await asyncio.to_thread(self.grants.upstream_values, req)
        code = request.query_params.get("code")
        if values is None or not code:
            await asyncio.to_thread(self.grants.drop_request, req)
            return error_page(
                "Sign-in was cancelled or failed at the identity provider. Start again from your app."
            )
        verifier, oidc_nonce = values
        try:
            email = await self.upstream.email(code, verifier, oidc_nonce)
        except (LoginRefused, httpx.HTTPError) as exc:
            await asyncio.to_thread(self.grants.drop_request, req)
            reason = (
                str(exc)
                if isinstance(exc, LoginRefused)
                else "The sign-in service is not reachable right now."
            )
            await asyncio.to_thread(
                self.audit.append, service="vault-mcp", tool="oauth_login", result="forbidden",
                client_id=pending.client_id, upstream=self.upstream.name, detail=reason,
            )  # fmt: skip
            return error_page(reason, 403)
        await asyncio.to_thread(self.grants.identify, req, email, "write")
        return RedirectResponse(f"/oauth/consent?req={req}", status_code=303, headers=_HEADERS)

    async def login(self, request: Request) -> Response:
        req = request.query_params.get("req")
        pending = await self._pending(req)
        if req is None or pending is None:
            return error_page(
                "This sign-in link has expired or is not valid. Start the connection again from your app."
            )
        if pending.owner is not None:
            return RedirectResponse(f"/oauth/consent?req={req}", status_code=303, headers=_HEADERS)
        return await self.login_start(request, req, pending)

    async def login_submit(self, request: Request) -> Response:
        if self.upstream is not None:
            return error_page(
                "Sign in with your organisation's account: start again from your app.", 405
            )
        form = await request.form()
        req, csrf, code = (str(form.get(k) or "") for k in ("req", "csrf", "code"))
        pending = await self._pending(req)
        if pending is None:
            return error_page(
                "This sign-in link has expired or is not valid. Start the connection again from your app."
            )
        if not self._same_browser(request, pending, csrf):
            return error_page(
                "This sign-in form was opened in another browser or has expired. Start again from your app."
            )
        reason = await asyncio.to_thread(self.grants.login_with_approval_code, req, code)
        if reason is not None:
            if await self._pending(req) is None:
                return error_page(reason, 429)
            return self._approval_form(req, reason)
        return RedirectResponse(f"/oauth/consent?req={req}", status_code=303, headers=_HEADERS)

    # -- consent ------------------------------------------------------------------------------

    async def consent(self, request: Request) -> Response:
        req = request.query_params.get("req") or ""
        pending = await self._pending(req)
        if pending is None or pending.owner is None or pending.max_scope is None:
            return error_page(
                "This consent link has expired or is not valid. Start the connection again from your app."
            )
        if not self.grants.cookie_matches(pending, request.cookies.get(COOKIE)):
            return error_page(
                "This consent page was opened in another browser. Start again from your app."
            )
        client = await asyncio.to_thread(self.grants.get_client, pending.client_id) or {}
        name = str(client.get("client_name") or "An unnamed app")
        redirect_host = urlparse(str(pending.params["redirect_uri"])).netloc or str(
            pending.params["redirect_uri"]
        )
        allowed = _allowed(pending)
        nonce, csrf = await asyncio.to_thread(self.grants.bind_browser, req)
        write_choice = (
            "<p><label><input type=radio name=scope value=write checked> Read and edit pages</label><br>"
            "<label><input type=radio name=scope value=read> Read only</label></p>"
            if allowed == "write"
            else "<input type=hidden name=scope value=read><p>Access: <strong>read only</strong></p>"
        )
        response = page(
            "Allow access to the docs vault?",
            f"<p><strong>{html.escape(name)}</strong> wants to use the docs vault as "
            f"<strong>{html.escape(pending.owner)}</strong>.</p>"
            f"<dl><dt>App</dt><dd>{html.escape(name)}</dd><dt>Sends you back to</dt><dd>{html.escape(redirect_host)}</dd>"
            f"<dt>Requested</dt><dd>{'read and edit' if _requested(pending) == 'write' else 'read only'}</dd></dl>"
            f"<form method=post action='/oauth/consent'>"
            f"<input type=hidden name=req value='{html.escape(req, quote=True)}'>"
            f"<input type=hidden name=csrf value='{html.escape(csrf, quote=True)}'>{write_choice}"
            f"<p><button type=submit name=action value=allow>Allow</button>"
            f"<button type=submit name=action value=deny>Deny</button></p></form>"
            f"<p>You can revoke this later: ask your administrator to run <code>scripts/token revoke</code>.</p>",
        )
        return self._with_cookie(response, nonce)

    async def consent_submit(self, request: Request) -> Response:
        form = await request.form()
        req, csrf, action, scope = (
            str(form.get(k) or "") for k in ("req", "csrf", "action", "scope")
        )
        pending = await self._pending(req)
        if pending is None or pending.owner is None:
            return error_page(
                "This consent link has expired or is not valid. Start the connection again from your app."
            )
        if not self._same_browser(request, pending, csrf):
            return error_page(
                "This consent form was opened in another browser or has expired. Start again from your app."
            )
        redirect_uri = str(pending.params["redirect_uri"])
        state = pending.params.get("state")
        await asyncio.to_thread(self.grants.drop_request, req)
        if action != "allow":
            target = construct_redirect_uri(redirect_uri, error="access_denied", state=state)
            return RedirectResponse(target, status_code=303, headers=_HEADERS)
        allowed = _allowed(pending)
        granted: Literal["read", "write"] = (
            "write" if scope == "write" and allowed == "write" else "read"
        )
        code = await asyncio.to_thread(
            self.grants.create_code, pending.client_id, pending.params, pending.owner, granted
        )
        client = await asyncio.to_thread(self.grants.get_client, pending.client_id) or {}
        await asyncio.to_thread(
            self.audit.append, service="vault-mcp", tool="oauth_consent", result="ok", owner=pending.owner,
            client_id=pending.client_id, client_name=client.get("client_name"), scope=granted,
        )  # fmt: skip
        target = construct_redirect_uri(redirect_uri, code=code, state=state)
        return RedirectResponse(target, status_code=303, headers=_HEADERS)

    def register(self, mcp: FastMCP[Any]) -> None:
        mcp.custom_route("/oauth/login", methods=["GET"])(self.login)
        mcp.custom_route("/oauth/login", methods=["POST"])(self.login_submit)
        mcp.custom_route("/oauth/consent", methods=["GET"])(self.consent)
        mcp.custom_route("/oauth/consent", methods=["POST"])(self.consent_submit)
        mcp.custom_route("/oauth/callback", methods=["GET"])(self.callback)


def _requested(pending: PendingRequest) -> Literal["read", "write"]:
    return scope_from_oauth(pending.params.get("scopes"))


def _allowed(pending: PendingRequest) -> Literal["read", "write"]:
    """The most the user may grant: what the app asked for, capped by the sign-in."""
    return "write" if _requested(pending) == "write" and pending.max_scope == "write" else "read"
