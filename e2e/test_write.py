"""SPEC §16.5 (write half), §16.9, §16.10 (refusal), §16.13 (MCP half), §16.15 on the real stack."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from harness import Stack


def _code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    return str(json.loads(str(exc.value))["code"])


async def _call(stack: Stack, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as client:
        out = (await client.call_tool(tool, args)).structured_content
    assert out is not None
    return out


def _writer(stack: Stack, name: str) -> str:
    return stack.token("create", "--name", name, "--owner", "e2e@example.com", "--scope", "write")


async def test_16_5_read_token_cannot_modify(stack: Stack) -> None:
    reader = stack.token(
        "create", "--name", "ro-writer-check", "--owner", "e2e@example.com", "--scope", "read"
    )
    with pytest.raises(ToolError) as exc:
        await _call(
            stack, reader, "write", {"path": "wiki/ro.md", "content": "x", "base_version": None}
        )
    assert _code(exc) == "forbidden"
    assert not (stack.root / "vault" / "wiki" / "ro.md").exists()


async def test_16_9_web_edit_between_read_and_write_is_a_conflict(stack: Stack) -> None:
    token = _writer(stack, "conflict-agent")
    page: Path = stack.root / "vault" / "wiki" / "conflict.md"
    created = await _call(
        stack, token, "write", {"path": "wiki/conflict.md", "content": "v1\n", "base_version": None}
    )
    page.write_text("edited in the browser\n")
    with pytest.raises(ToolError) as exc:
        await _call(
            stack,
            token,
            "write",
            {"path": "wiki/conflict.md", "content": "agent\n", "base_version": created["version"]},
        )
    assert _code(exc) == "conflict"
    assert page.read_text() == "edited in the browser\n"


async def test_16_10_agent_write_to_a_human_page_is_refused(stack: Stack) -> None:
    token = _writer(stack, "human-page-agent")
    page = stack.root / "vault" / "projects" / "alpha.md"
    before = page.read_text()
    with pytest.raises(ToolError) as exc:
        await _call(stack, token, "append", {"path": "projects/alpha.md", "text": "agent note\n"})
    assert _code(exc) == "forbidden"
    assert page.read_text() == before


async def test_16_13_secret_is_rejected_over_mcp(stack: Stack) -> None:
    token = _writer(stack, "secret-agent")
    fake = "AKIA" + "Q3RTZ7XK2P4LMNBV"
    with pytest.raises(ToolError) as exc:
        await _call(
            stack,
            token,
            "write",
            {"path": "wiki/keys.md", "content": f"key {fake}\n", "base_version": None},
        )
    assert _code(exc) == "secret_detected"
    assert fake not in str(exc.value)
    assert not (stack.root / "vault" / "wiki" / "keys.md").exists()


async def test_16_15_every_mcp_write_has_an_audit_entry_and_a_history_version(stack: Stack) -> None:
    token = _writer(stack, "history-agent")
    version = None
    for i in range(5):
        version = (
            await _call(
                stack,
                token,
                "write",
                {"path": "wiki/audited.md", "content": f"v{i}\n", "base_version": version},
            )
        )["version"]
    rows = [json.loads(x) for x in (stack.root / "data" / "audit.log").read_text().splitlines()]
    ok_ids = {
        r["id"]
        for r in rows
        if r["tool"] == "write" and r["result"] == "ok" and r.get("path") == "wiki/audited.md"
    }
    pointer_dir = stack.root / "data" / "history" / "wiki" / "audited.md"
    pointers = [json.loads(p.read_text()) for p in sorted(pointer_dir.glob("*.json"))]
    mcp_ids = {p["audit_id"] for p in pointers if p["source"] == "mcp"}
    assert len(ok_ids) == 5
    assert ok_ids == mcp_ids
