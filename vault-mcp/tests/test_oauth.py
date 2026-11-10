"""OAuth 2.1 authorization server (SPEC §6.3 (2), §16.6 local part; plan D16/D17)."""

import base64
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from vault_core.config import Settings
from vault_mcp.oauth.grants import OAuthStore
from vault_mcp.server import build_server
from vault_mcp.store import CredentialStore

REDIRECT = "http://127.0.0.1:9/callback"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Server:
    url: str
    store: CredentialStore
    data: Path

    def approval_code(self, owner: str = "alice@example.com", scope: str = "write") -> str:
        from datetime import timedelta

        return OAuthStore(self.store).create_approval_code(owner, scope, timedelta(hours=1))  # type: ignore[arg-type]


def _serve(settings: Settings) -> Iterator[str]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    settings = settings.model_copy(
        update={"mcp_oauth_issuer": url} if settings.mcp_oauth_enabled else {}
    )
    app = build_server(settings).http_app(path="/mcp")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def server(vault: Path, data: Path, policy_file: Path) -> Iterator[Server]:
    settings = Settings(
        vault_dir=vault, data_dir=data, policy_file=policy_file, mcp_oauth_issuer="x"
    )
    for url in _serve(settings):
        yield Server(url, CredentialStore(data / "tokens.db"), data)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def form_field(html: str, name: str) -> str:
    match = re.search(rf"name={name} value='([^']*)'", html)
    assert match, f"no {name} field in page"
    return match.group(1)


def register(
    http: httpx.Client, base: str, redirect: str = REDIRECT, name: str = "Test App"
) -> dict[str, Any]:
    r = http.post(
        f"{base}/register",
        json={"redirect_uris": [redirect], "client_name": name, "token_endpoint_auth_method": "none",
              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return dict(r.json())


def start(
    http: httpx.Client,
    base: str,
    client: dict[str, Any],
    challenge: str,
    scope: str = "vault:read vault:write",
) -> str:
    """GET /authorize; returns the login page URL."""
    q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "state": "st8",
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope}  # fmt: skip
    r = http.get(f"{base}/authorize?{urlencode(q)}")
    assert r.status_code == 302, r.text
    return str(r.headers["location"])


def sign_in(http: httpx.Client, login_url: str, code: str) -> httpx.Response:
    page = http.get(login_url)
    assert page.status_code == 200
    req = parse_qs(urlparse(login_url).query)["req"][0]
    return http.post(
        login_url.split("?")[0],
        data={"req": req, "csrf": form_field(page.text, "csrf"), "code": code},
    )


def consent(
    http: httpx.Client, base: str, consent_path: str, action: str = "allow", scope: str = "write"
) -> httpx.Response:
    page = http.get(base + consent_path)
    assert page.status_code == 200, page.text
    req = form_field(page.text, "req")
    return http.post(
        f"{base}/oauth/consent",
        data={"req": req, "csrf": form_field(page.text, "csrf"), "action": action, "scope": scope},
    )


def full_flow(
    server: Server, *, grant_scope: str = "write", code_scope: str = "write"
) -> tuple[dict[str, Any], dict[str, Any], str]:
    http = httpx.Client(follow_redirects=False)
    client = register(http, server.url)
    verifier, challenge = pkce()
    login = start(http, server.url, client, challenge)
    signed = sign_in(http, login, server.approval_code(scope=code_scope))
    assert signed.status_code == 303, signed.text
    done = consent(http, server.url, signed.headers["location"], scope=grant_scope)
    assert done.status_code == 303
    params = parse_qs(urlparse(done.headers["location"]).query)
    assert params["state"] == ["st8"]
    token = http.post(
        f"{server.url}/token",
        data={"grant_type": "authorization_code", "code": params["code"][0], "redirect_uri": REDIRECT,
              "client_id": client["client_id"], "code_verifier": verifier},
    )  # fmt: skip
    assert token.status_code == 200, token.text
    return client, dict(token.json()), verifier


async def call(base: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    async with Client(f"{base}/mcp", auth=token) as c:
        return (await c.call_tool(tool, args)).structured_content


def refresh(base: str, client: dict[str, Any], refresh_token: str) -> httpx.Response:
    return httpx.post(
        f"{base}/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
        },
    )


# -- metadata and registration ------------------------------------------------------------------


def test_metadata_advertises_pkce_s256_public_clients_and_root_endpoints(server: Server) -> None:
    meta = httpx.get(f"{server.url}/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == server.url
    assert meta["token_endpoint"] == f"{server.url}/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    prm = httpx.get(f"{server.url}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["authorization_servers"][0].rstrip("/") == server.url
    unauth = httpx.post(f"{server.url}/mcp", json={})
    assert unauth.status_code == 401
    assert "resource_metadata" in unauth.headers["www-authenticate"]


@pytest.mark.parametrize(
    "uri", ["http://evil.example/cb", "javascript:alert(1)", "https://app.example/cb#frag"]
)
def test_registration_rejects_unsafe_redirect_uris(server: Server, uri: str) -> None:
    r = httpx.post(
        f"{server.url}/register",
        json={"redirect_uris": [uri], "client_name": "x", "token_endpoint_auth_method": "none"},
    )
    assert r.status_code == 400


# -- the flow ------------------------------------------------------------------------------------


async def test_full_flow_issues_a_grant_listed_with_its_owner_that_can_call_tools(
    server: Server,
    watcher: object,
) -> None:
    _, tokens, _ = full_flow(server)
    assert tokens["scope"] == "vault:read vault:write"
    assert tokens["token_type"] == "Bearer"
    out = await call(
        server.url,
        tokens["access_token"],
        "append",
        {"path": "wiki/index.md", "text": "via oauth\n"},
    )
    assert out is not None
    grants = [c for c in server.store.list_credentials() if c.kind == "oauth"]
    assert [(g.owner, g.scope, g.name.startswith("oauth:Test-App-")) for g in grants] == [
        ("alice@example.com", "write", True)
    ]
    audit = [json.loads(x) for x in (server.data / "audit.log").read_text().splitlines()]
    assert any(r["tool"] == "append" and r.get("credential_id") == grants[0].id for r in audit)


async def test_consent_can_narrow_to_read_and_read_grants_cannot_write(server: Server) -> None:
    _, tokens, _ = full_flow(server, grant_scope="read")
    assert tokens["scope"] == "vault:read"
    await call(server.url, tokens["access_token"], "read", {"path": "wiki/index.md"})
    with pytest.raises(ToolError) as exc:
        await call(
            server.url, tokens["access_token"], "append", {"path": "wiki/index.md", "text": "x\n"}
        )
    assert json.loads(str(exc.value))["code"] == "forbidden"


def test_a_read_only_approval_code_caps_the_grant_even_if_write_is_posted(server: Server) -> None:
    _, tokens, _ = full_flow(server, code_scope="read", grant_scope="write")
    assert tokens["scope"] == "vault:read"


def test_deny_redirects_with_access_denied(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    login = start(http, server.url, client, pkce()[1])
    signed = sign_in(http, login, server.approval_code())
    denied = consent(http, server.url, signed.headers["location"], action="deny")
    assert parse_qs(urlparse(denied.headers["location"]).query) == {
        "error": ["access_denied"],
        "state": ["st8"],
    }
    assert [c for c in server.store.list_credentials() if c.kind == "oauth"] == []


def test_wrong_pkce_verifier_is_rejected(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    _, challenge = pkce()
    signed = sign_in(http, start(http, server.url, client, challenge), server.approval_code())
    done = consent(http, server.url, signed.headers["location"])
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    r = http.post(f"{server.url}/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                                  "client_id": client["client_id"], "code_verifier": pkce()[0]})  # fmt: skip
    assert r.status_code == 401  # FastMCP maps invalid_grant to 401 (MCP spec)
    assert r.json()["error"] == "invalid_grant"


def test_authorization_codes_are_single_use(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    verifier, challenge = pkce()
    signed = sign_in(http, start(http, server.url, client, challenge), server.approval_code())
    code = parse_qs(
        urlparse(consent(http, server.url, signed.headers["location"]).headers["location"]).query
    )["code"][0]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": verifier,
    }
    assert http.post(f"{server.url}/token", data=body).status_code == 200
    assert http.post(f"{server.url}/token", data=body).status_code == 401


# -- approval codes ------------------------------------------------------------------------------


def test_an_approval_code_works_once(server: Server) -> None:
    code = server.approval_code()
    http = httpx.Client()
    client = register(http, server.url)
    assert sign_in(http, start(http, server.url, client, pkce()[1]), code).status_code == 303
    again = sign_in(http, start(http, server.url, client, pkce()[1]), code)
    assert again.status_code == 200
    assert "not valid, already used, or expired" in again.text


def test_codes_are_accepted_with_lowercase_and_without_the_dash(server: Server) -> None:
    code = server.approval_code().replace("-", "").lower()
    http = httpx.Client()
    client = register(http, server.url)
    assert sign_in(http, start(http, server.url, client, pkce()[1]), code).status_code == 303


def test_five_wrong_codes_kill_the_request(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    login = start(http, server.url, client, pkce()[1])
    for _ in range(4):
        assert sign_in(http, login, "WRONG-CODE0").status_code == 200
    page = http.get(login)
    req = parse_qs(urlparse(login).query)["req"][0]
    last = http.post(
        f"{server.url}/oauth/login",
        data={"req": req, "csrf": form_field(page.text, "csrf"), "code": "WRONG-CODE0"},
    )
    assert last.status_code == 429
    assert http.get(login).status_code == 400  # the request is gone: start again


def test_another_browser_cannot_complete_someone_elses_request(server: Server) -> None:
    alice, mallory = httpx.Client(), httpx.Client()
    client = register(alice, server.url)
    signed = sign_in(alice, start(alice, server.url, client, pkce()[1]), server.approval_code())
    consent_url = server.url + signed.headers["location"]
    assert mallory.get(consent_url).status_code == 400
    page = alice.get(consent_url)
    stolen = {
        "req": form_field(page.text, "req"),
        "csrf": form_field(page.text, "csrf"),
        "action": "allow",
        "scope": "write",
    }
    assert mallory.post(f"{server.url}/oauth/consent", data=stolen).status_code == 400


def test_consent_requires_sign_in_first(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    login = start(http, server.url, client, pkce()[1])
    http.get(login)
    req = parse_qs(urlparse(login).query)["req"][0]
    assert http.get(f"{server.url}/oauth/consent?req={req}").status_code == 400


def test_pages_cannot_be_framed_and_are_not_cached(server: Server) -> None:
    http = httpx.Client()
    client = register(http, server.url)
    page = http.get(start(http, server.url, client, pkce()[1]))
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert "httponly" in page.headers["set-cookie"].lower()


# -- refresh rotation and revocation ----------------------------------------------------------------


async def test_refresh_rotates_and_reuse_revokes_the_whole_grant(server: Server) -> None:
    client, tokens, _ = full_flow(server)
    first = refresh(server.url, client, tokens["refresh_token"])
    assert first.status_code == 200
    new = first.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    await call(server.url, new["access_token"], "read", {"path": "wiki/index.md"})
    replay = refresh(server.url, client, tokens["refresh_token"])  # a stolen copy of the old token
    assert replay.status_code == 401
    assert replay.json()["error"] == "invalid_grant"
    with pytest.raises(httpx.HTTPStatusError):  # the legitimate holder is cut off too
        await call(server.url, new["access_token"], "read", {"path": "wiki/index.md"})
    assert refresh(server.url, client, new["refresh_token"]).status_code == 401
    assert [
        c.status(server.store.now()) for c in server.store.list_credentials() if c.kind == "oauth"
    ] == ["revoked"]


async def test_token_revoke_stops_access_and_refresh_immediately(server: Server) -> None:
    client, tokens, _ = full_flow(server)
    grant = next(c for c in server.store.list_credentials() if c.kind == "oauth")
    server.store.revoke(grant.name)
    with pytest.raises(httpx.HTTPStatusError):
        await call(server.url, tokens["access_token"], "read", {"path": "wiki/index.md"})
    assert refresh(server.url, client, tokens["refresh_token"]).status_code == 401


async def test_rfc7009_revoke_endpoint_ends_the_grant(server: Server) -> None:
    client, tokens, _ = full_flow(server)
    r = httpx.Client().post(
        f"{server.url}/revoke",
        data={
            "token": tokens["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": "",
        },
    )
    assert r.status_code == 200
    with pytest.raises(httpx.HTTPStatusError):
        await call(server.url, tokens["access_token"], "read", {"path": "wiki/index.md"})


async def test_bearer_tokens_keep_working_alongside_oauth(server: Server) -> None:
    token, _ = server.store.create_bearer(name="cli", owner="bob@example.com", scope="read")
    assert await call(server.url, token, "read", {"path": "wiki/index.md"})


# -- the MCP SDK client runs the whole flow --------------------------------------------------------


class MemoryStorage:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


async def test_mcp_sdk_client_completes_the_flow_headlessly(server: Server) -> None:
    browser = httpx.AsyncClient()
    result: dict[str, str] = {}
    approval = server.approval_code()

    async def redirect_handler(url: str) -> None:  # stands in for the user's browser
        login = await browser.get(url, follow_redirects=True)
        req = form_field(login.text, "req")
        signed = await browser.post(
            f"{server.url}/oauth/login",
            data={"req": req, "csrf": form_field(login.text, "csrf"), "code": approval},
        )
        page = await browser.get(server.url + signed.headers["location"])
        done = await browser.post(
            f"{server.url}/oauth/consent",
            data={
                "req": req,
                "csrf": form_field(page.text, "csrf"),
                "action": "allow",
                "scope": "write",
            },
        )
        q = parse_qs(urlparse(done.headers["location"]).query)
        result.update(code=q["code"][0], state=q["state"][0])

    async def callback_handler() -> tuple[str, str | None]:
        return result["code"], result["state"]

    metadata = OAuthClientMetadata.model_validate(
        {"client_name": "SDK client", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
         "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    )  # fmt: skip
    auth = OAuthClientProvider(
        f"{server.url}/mcp",
        metadata,
        MemoryStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with Client(StreamableHttpTransport(f"{server.url}/mcp", auth=auth)) as c:
        out = await c.call_tool("read", {"path": "wiki/index.md"})
    assert out.structured_content is not None
    assert [c.name.startswith("oauth:SDK-client-") for c in server.store.list_credentials()] == [
        True
    ]


# -- switched off ------------------------------------------------------------------------------------


def test_mcp_oauth_enabled_false_disables_every_oauth_route(
    vault: Path, data: Path, policy_file: Path
) -> None:
    settings = Settings(
        vault_dir=vault,
        data_dir=data,
        policy_file=policy_file,
        mcp_oauth_enabled=False,
        mcp_oauth_issuer="x",
    )
    for url in _serve(settings):
        for path in (
            "/.well-known/oauth-authorization-server",
            "/register",
            "/authorize",
            "/token",
            "/oauth/login",
        ):
            assert httpx.get(url + path).status_code in (404, 405), path
        token, _ = CredentialStore(data / "tokens.db").create_bearer(
            name="b", owner="b@example.com", scope="read"
        )
        assert (
            httpx.post(
                f"{url}/mcp", headers={"Authorization": f"Bearer {token}"}, json={}
            ).status_code
            != 401
        )


# -- store-level guards -------------------------------------------------------------------------------


def test_thirty_failed_sign_ins_lock_every_request_for_ten_minutes(data: Path) -> None:
    from datetime import UTC, datetime, timedelta

    clock = {"now": datetime(2026, 11, 10, tzinfo=UTC)}
    grants = OAuthStore(CredentialStore(data / "tokens.db", now=lambda: clock["now"]))
    good = grants.create_approval_code("a@example.com", "read", timedelta(hours=2))
    for i in range(30):
        req = grants.create_request("client", {"redirect_uri": REDIRECT})
        assert grants.login_with_approval_code(req, f"WRONG{i:05d}") is not None
    req = grants.create_request("client", {"redirect_uri": REDIRECT})
    assert "Too many failed sign-in attempts" in str(grants.login_with_approval_code(req, good))
    clock["now"] += timedelta(minutes=11)
    req = grants.create_request("client", {"redirect_uri": REDIRECT})
    assert grants.login_with_approval_code(req, good) is None


def test_a_version_1_tokens_db_is_migrated_and_keeps_its_bearer_tokens(data: Path) -> None:
    import sqlite3

    from vault_mcp import store as store_module

    path = data / "tokens.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN; {store_module._SCHEMA} PRAGMA user_version=1; COMMIT;")
    conn.close()
    store = CredentialStore(path)
    token, _ = store.create_bearer(name="old", owner="o@example.com", scope="read")
    assert store.verify(token) is not None
    assert OAuthStore(store).create_approval_code(
        "o@example.com", "read", __import__("datetime").timedelta(hours=1)
    )
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_cli_approve_code_prints_a_usable_code(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from vault_mcp.cli import main

    assert (
        main(
            [
                "--data-dir",
                str(data),
                "oauth",
                "approve-code",
                "--owner",
                "c@example.com",
                "--scope",
                "write",
                "--ttl",
                "30m",
            ]
        )
        == 0
    )
    code = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[0-9A-Z]{5}-[0-9A-Z]{5}", code)
    grants = OAuthStore(CredentialStore(data / "tokens.db"))
    req = grants.create_request("client", {"redirect_uri": REDIRECT})
    assert grants.login_with_approval_code(req, code) is None
    pending = grants.get_request(req)
    assert pending is not None
    assert (pending.owner, pending.max_scope) == ("c@example.com", "write")
    assert (
        main(
            [
                "--data-dir",
                str(data),
                "oauth",
                "approve-code",
                "--owner",
                "c@example.com",
                "--ttl",
                "30d",
            ]
        )
        == 1
    )


async def test_oauth_use_is_shown_as_last_used_in_token_list(server: Server) -> None:
    _, tokens, _ = full_flow(server)
    await call(server.url, tokens["access_token"], "read", {"path": "wiki/index.md"})
    grant = next(c for c in server.store.list_credentials() if c.kind == "oauth")
    assert grant.last_used_at is not None
