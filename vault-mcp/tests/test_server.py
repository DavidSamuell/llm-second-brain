"""The server over real transports: streamable HTTP (uvicorn on a free port) and stdio."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from vault_core.config import Settings
from vault_mcp.server import build_server
from vault_mcp.store import CredentialStore


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def settings(vault: Path, data: Path, policy_file: Path) -> Settings:
    return Settings(vault_dir=vault, data_dir=data, policy_file=policy_file)


@pytest.fixture
def base_url(settings: Settings) -> Iterator[str]:
    port = _free_port()
    app = build_server(settings).http_app(path="/mcp")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_health_needs_no_token(base_url: str) -> None:
    r = httpx.get(f"{base_url}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("auth", [None, "Bearer vmcp_" + "A" * 43, "Bearer garbage"])
def test_mcp_without_a_valid_token_is_401_with_www_authenticate(
    base_url: str, auth: str | None
) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    r = httpx.post(f"{base_url}/mcp", headers=headers, json=body)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_revoked_token_is_401(base_url: str, store: CredentialStore) -> None:
    token, _ = store.create_bearer(name="r", owner="r@example.com", scope="read")
    store.revoke("r")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    r = httpx.post(
        f"{base_url}/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    assert r.status_code == 401


async def test_bearer_client_can_read_list_and_get_schema(
    base_url: str, store: CredentialStore, data: Path
) -> None:
    token, _ = store.create_bearer(
        name="alice", owner="alice@example.com", scope="read", folders=["wiki/"]
    )
    async with Client(f"{base_url}/mcp", auth=token) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"get_schema", "read", "list"} <= names
        page = (await client.call_tool("read", {"path": "wiki/index.md"})).structured_content
        assert page is not None
        assert page["path"] == "wiki/index.md"
        listing = (await client.call_tool("list", {"folder": "wiki"})).structured_content
        assert listing is not None
        assert "wiki/index.md" in [e["path"] for e in listing["entries"]]
        with pytest.raises(ToolError) as exc:
            await client.call_tool("read", {"path": "projects/alpha.md"})
        assert json.loads(str(exc.value))["code"] == "forbidden"
    rows = [json.loads(line) for line in (data / "audit.log").read_text().splitlines()]
    calls = [(r["tool"], r["result"], r.get("path")) for r in rows]
    assert ("read", "ok", "wiki/index.md") in calls
    assert ("read", "forbidden", "projects/alpha.md") in calls
    assert all(r["credential_name"] == "alice" and r["owner"] == "alice@example.com" for r in rows)


def _stdio(env: dict[str, str]) -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=["-m", "vault_mcp.cli", "serve", "--stdio"],
        env={**os.environ, **env},
    )


async def test_stdio_mode_uses_the_vault_mcp_token(
    vault: Path, data: Path, policy_file: Path, store: CredentialStore
) -> None:
    token, _ = store.create_bearer(name="local", owner="ops@example.com", scope="read")
    env = {
        "VAULT_DIR": str(vault),
        "DATA_DIR": str(data),
        "POLICY_FILE": str(policy_file),
        "VAULT_MCP_TOKEN": token,
    }
    async with Client(_stdio(env)) as client:
        out = (await client.call_tool("get_schema", {})).structured_content
        assert out is not None
        assert out["schema_version"] == 3


def test_stdio_mode_refuses_an_invalid_token(vault: Path, data: Path, policy_file: Path) -> None:
    env = {
        **os.environ,
        "VAULT_DIR": str(vault),
        "DATA_DIR": str(data),
        "POLICY_FILE": str(policy_file),
        "VAULT_MCP_TOKEN": "nope",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "vault_mcp.cli", "serve", "--stdio"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "VAULT_MCP_TOKEN" in proc.stderr


async def test_unexpected_tool_failures_are_audited_without_details(
    vault: Path, data: Path, policy_file: Path, store: CredentialStore
) -> None:
    token, _ = store.create_bearer(name="local", owner="ops@example.com", scope="read")
    page = vault / "wiki" / "index.md"
    page.chmod(0o000)
    try:
        settings = Settings(vault_dir=vault, data_dir=data, policy_file=policy_file)
        async with Client(build_server(settings, stdio_token=token)) as client:
            with pytest.raises(ToolError):
                await client.call_tool("read", {"path": "wiki/index.md"})
    finally:
        page.chmod(0o644)
    (row,) = [json.loads(x) for x in (data / "audit.log").read_text().splitlines()]
    assert (row["tool"], row["result"], row["path"]) == ("read", "internal_error", "wiki/index.md")
    assert "detail" not in row
