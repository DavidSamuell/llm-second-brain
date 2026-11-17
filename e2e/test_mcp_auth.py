"""SPEC §16.5 (read half): missing, invalid, and revoked tokens get 401; a read token reads."""

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from harness import Stack

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _post(stack: Stack, authorization: str | None) -> httpx.Response:
    headers = dict(HEADERS)
    if authorization:
        headers["Authorization"] = authorization
    return httpx.post(f"{stack.mcp_url}/mcp", headers=headers, json=INIT)


@pytest.mark.parametrize("authorization", [None, "Bearer vmcp_" + "A" * 43, "Bearer not-a-token"])
def test_16_5_missing_or_invalid_token_is_401(stack: Stack, authorization: str | None) -> None:
    r = _post(stack, authorization)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_16_5_revoked_token_is_401(stack: Stack) -> None:
    token = stack.token(
        "create", "--name", "to-revoke", "--owner", "e2e@example.com", "--scope", "read"
    )
    assert _post(stack, f"Bearer {token}").status_code == 200
    stack.token_cmd("revoke", "to-revoke")
    assert _post(stack, f"Bearer {token}").status_code == 401


async def test_16_5_read_token_reads_within_policy(stack: Stack) -> None:
    token = stack.token(
        "create",
        "--name",
        "reader",
        "--owner",
        "e2e@example.com",
        "--scope",
        "read",
        "--folders",
        "wiki/",
    )
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as client:
        schema = (await client.call_tool("get_schema", {})).structured_content
        assert schema is not None
        assert schema["schema_version"] == 1
        page = (await client.call_tool("read", {"path": "wiki/index.md"})).structured_content
        assert page is not None
        assert page["content"].startswith("---\ntitle: Wiki index")
        for path in ("private/salaries.md", "projects/alpha.md"):
            with pytest.raises(ToolError) as exc:
                await client.call_tool("read", {"path": path})
            assert json.loads(str(exc.value))["code"] == "forbidden"
    audit = (stack.root / "data" / "audit.log").read_text().splitlines()
    assert any(json.loads(line)["credential_name"] == "reader" for line in audit)
