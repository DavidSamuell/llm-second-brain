"""vault-mcp server (SPEC §6.2-6.4): streamable HTTP at `/mcp` with bearer auth, stdio mode
authenticated by `VAULT_MCP_TOKEN`, and an unauthenticated `/health`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp
