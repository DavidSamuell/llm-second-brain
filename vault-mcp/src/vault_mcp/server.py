"""vault-mcp server (SPEC §6.2-6.4): streamable HTTP at `/mcp` with bearer auth, stdio mode
authenticated by `VAULT_MCP_TOKEN`, and an unauthenticated `/health`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse

from vault_core.audit import AuditLog
from vault_core.config import Settings
from vault_core.policy import load_policy
from vault_mcp.errors import VaultToolError
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.tools_read import ReadTools
from vault_mcp.tools_search import SearchTools
from vault_mcp.writer import VaultWriter

T = TypeVar("T")


class BearerVerifier(TokenVerifier):
    """Resolves `Authorization: Bearer vmcp_…` against tokens.db on every request, so a
    revoked or expired credential stops working immediately."""

    def __init__(self, store: CredentialStore) -> None:
        super().__init__()
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        cred = await asyncio.to_thread(self._store.verify, token)
        if cred is None:
            return None
        return AccessToken(
            token=token,
            client_id=cred.id,
            scopes=[cred.scope],
            expires_at=int(cred.expires_at.timestamp()) if cred.expires_at else None,
            claims={"credential_id": cred.id},
        )


def build_server(settings: Settings, *, stdio_token: str | None = None) -> FastMCP:
    """HTTP mode authenticates each request; stdio mode re-verifies `stdio_token` on every
    call, so revoking it takes effect without restarting the agent's server process."""
    policy = load_policy(settings.policy_file)
    store = CredentialStore(settings.data_dir / "tokens.db")
    audit = AuditLog(settings.data_dir / "audit.log")
    reads = ReadTools(settings.vault_dir, policy)
    searches = SearchTools(settings.data_dir / "index.db", policy)
    writer = VaultWriter(
        settings.vault_dir, settings.data_dir, policy, ack_timeout_s=settings.history_ack_timeout
    )

    mcp = FastMCP(
        "vault-mcp",
        instructions="Shared documentation vault. Call get_schema first: AGENTS.md holds the rules.",
        auth=None if stdio_token is not None else BearerVerifier(store),
    )

    def current() -> Credential:
        if stdio_token is not None:
            stdio_cred = store.verify(stdio_token)
            if stdio_cred is None:
                raise VaultToolError("forbidden", "VAULT_MCP_TOKEN is invalid, expired, or revoked")
            return stdio_cred
        token = get_access_token()
        cred_id = token.claims.get("credential_id") if token and token.claims else None
        cred = store.get(cred_id) if cred_id else None
        if cred is None:  # the HTTP auth middleware should make this unreachable
            raise VaultToolError("forbidden", "no authenticated credential")
        return cred

    def audited(tool: str, path: str | None, call: Callable[[Credential], T]) -> T:
        cred = current()
        fields: dict[str, Any] = {
            "credential_id": cred.id,
            "credential_name": cred.name,
            "owner": cred.owner,
            "path": path,
        }
        try:
            result = call(cred)
        except VaultToolError as exc:
            audit.append(service="vault-mcp", tool=tool, result=exc.code, **fields)
            raise
        except Exception:
            # No exception text: it can carry vault content or host paths.
            audit.append(service="vault-mcp", tool=tool, result="internal_error", **fields)
            raise
        audit.append(service="vault-mcp", tool=tool, result="ok", **fields)
        return result

    async def audited_write(tool: str, path: str, call: Callable[[Credential], Awaitable[T]]) -> T:
        """Write tools: the writer itself logs the durable `ok` line (D21 step 4) under the
        same id as the history version; only refusals and failures are logged here."""
        cred = current()
        fields: dict[str, Any] = {
            "credential_id": cred.id,
            "credential_name": cred.name,
            "owner": cred.owner,
            "path": path,
        }
        try:
            return await call(cred)
        except VaultToolError as exc:
            audit.append(service="vault-mcp", tool=tool, result=exc.code, **fields)
            raise
        except Exception:
            audit.append(service="vault-mcp", tool=tool, result="internal_error", **fields)
            raise

    @mcp.tool(name="get_schema")
    def get_schema() -> dict[str, Any]:
        """Return the vault's AGENTS.md (with schema_version). Read it before anything else."""
        return audited("get_schema", "AGENTS.md", reads.get_schema)

    @mcp.tool(name="read")
    def read(path: str) -> dict[str, Any]:
        """Read a file. Returns content (or content_base64 for binary files) and its version
        (sha256); pass that version as base_version when you write."""
        return audited("read", path, lambda cred: reads.read(cred, path))

    @mcp.tool(name="list")
    def list_(folder: str = "") -> dict[str, Any]:
        """List one folder ("" for the vault root) with a frontmatter summary per Markdown page."""
        return audited("list", folder or "/", lambda cred: reads.list_folder(cred, folder))

    @mcp.tool(name="search")
    def search(query: str, folder: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Full-text search (title, headings, path, frontmatter, body). Words are matched
        literally and all must appear; `folder` narrows the search to one folder."""
        return audited("search", folder, lambda cred: searches.search(cred, query, folder, limit))

    @mcp.tool(name="write")
    async def write(path: str, content: str, base_version: str | None) -> dict[str, Any]:
        """Create a file (base_version null) or replace it (base_version = the version you
        read). Fails with `conflict` if the file changed since you read it."""
        return await audited_write(
            "write", path, lambda cred: writer.write(cred, path, content, base_version)
        )

    @mcp.tool(name="append")
    async def append(path: str, text: str) -> dict[str, Any]:
        """Append text to an existing file, starting on a new line."""
        return await audited_write("append", path, lambda cred: writer.append(cred, path, text))

    @mcp.tool(name="replace_section")
    async def replace_section(
        path: str, heading: str, content: str, base_version: str
    ) -> dict[str, Any]:
        """Replace the body under one heading (subsections included), keeping the heading
        line. `heading` is its text ("Usage") or full line ("## Usage"). Prefer this over
        rewriting whole pages."""
        return await audited_write(
            "replace_section",
            path,
            lambda cred: writer.replace_section(cred, path, heading, content, base_version),
        )

    @mcp.tool(name="insert_after")
    async def insert_after(path: str, heading: str, content: str) -> dict[str, Any]:
        """Insert content directly below a heading, before the section's existing body."""
        return await audited_write(
            "insert_after", path, lambda cred: writer.insert_after(cred, path, heading, content)
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp
