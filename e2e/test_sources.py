"""SPEC §16.11: concurrent claims, and an expired lease returns the file to raw/."""

import asyncio
import json
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError
from harness import Stack


async def test_16_11_exactly_one_concurrent_claim_wins(stack: Stack) -> None:
    a = stack.token("create", "--name", "claimer-a", "--owner", "a@example.com", "--scope", "write")
    b = stack.token("create", "--name", "claimer-b", "--owner", "b@example.com", "--scope", "write")
    async with (
        Client(f"{stack.mcp_url}/mcp", auth=a) as ca,
        Client(f"{stack.mcp_url}/mcp", auth=b) as cb,
    ):
        sub = (
            await ca.call_tool(
                "submit_source", {"title": "Race", "content": "# Race\n", "origin": "e2e"}
            )
        ).structured_content
        assert sub is not None
        results = await asyncio.gather(
            ca.call_tool("claim_source", {"path": sub["path"]}),
            cb.call_tool("claim_source", {"path": sub["path"]}),
            return_exceptions=True,
        )
    errors = [r for r in results if isinstance(r, ToolError)]
    assert len(errors) == 1
    assert json.loads(str(errors[0]))["code"] == "already_claimed"


async def test_16_11_expired_lease_returns_the_file_to_raw(stack: Stack) -> None:
    token = stack.token(
        "create", "--name", "expirer", "--owner", "e@example.com", "--scope", "write"
    )
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as client:
        sub = (
            await client.call_tool(
                "submit_source", {"title": "Expire", "content": "x\n", "origin": "e2e"}
            )
        ).structured_content
        assert sub is not None
        await client.call_tool("claim_source", {"path": sub["path"], "ttl_minutes": 1})
    original: Path = stack.root / "vault" / sub["path"]
    assert not original.exists()
    deadline = time.monotonic() + 150  # 1-minute lease + 60 s sweep interval + margin
    while not original.exists():
        assert time.monotonic() < deadline, "expired lease was not returned"
        await asyncio.sleep(2)
