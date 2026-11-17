"""Write-policy acceptance against the real MCP/watcher stack, in both profiles."""

import asyncio
import json
import time

import pytest
import yaml
from fastmcp.exceptions import ToolError
from harness import Stack
from test_write import _call, _writer


async def test_write_policy_human_folders_keep_history_and_audit(stack: Stack) -> None:
    token = _writer(stack, "policy-folders")
    for rel in ("projects/policy-new.md", "pipelines/policy-etl.md"):
        result = await _call(
            stack, token, "write", {"path": rel, "content": "# New\n", "base_version": None}
        )
        assert (stack.root / "vault" / rel).read_text() == "# New\n"
        pointers = [
            json.loads(p.read_text())
            for p in (stack.root / "data" / "history" / rel).glob("*.json")
        ]
        rows = [
            json.loads(line)
            for line in (stack.root / "data" / "audit.log").read_text().splitlines()
        ]
        audit_ids = {
            row["id"]
            for row in rows
            if row.get("path") == rel and row["result"] == "ok" and row["tool"] == "write"
        }
        assert any(
            p["source"] == "mcp" and p["audit_id"] in audit_ids and p["sha256"] == result["version"]
            for p in pointers
        )


async def test_write_policy_three_human_edits_have_one_notice(stack: Stack) -> None:
    token = _writer(stack, "policy-notice")
    rel = "projects/policy-human.md"
    await _call(
        stack,
        token,
        "write",
        {
            "path": rel,
            "content": "---\nmaintainer: human\n---\n# Human\n",
            "base_version": None,
            "summary": "Create project documentation",
        },
    )
    for i in range(3):
        await _call(
            stack,
            token,
            "append",
            {
                "path": rel,
                "text": f"Finding {i}\n",
                "summary": f"Document finding {i}",
            },
        )
    queue = (stack.root / "vault" / "_system" / "review-queue.md").read_text()
    notices = [
        line
        for line in queue.splitlines()
        if "[[projects/policy-human]]" in line and "#review/agent-edit" in line
    ]
    assert len(notices) == 1
    rows = [
        json.loads(line) for line in (stack.root / "data" / "audit.log").read_text().splitlines()
    ]
    edits = [
        r for r in rows if r.get("path") == rel and r["tool"] == "append" and r["result"] == "ok"
    ]
    assert [r["summary"] for r in edits] == [f"Document finding {i}" for i in range(3)]
    assert all(r["human_page"] is True for r in edits)


async def test_write_policy_rule_write_and_move_are_protected(stack: Stack) -> None:
    token = _writer(stack, "policy-protected")
    rules = await _call(stack, token, "read", {"path": "AGENTS.md"})
    page = await _call(
        stack,
        token,
        "write",
        {
            "path": "wiki/policy-move.md",
            "content": "# Keep\n",
            "base_version": None,
        },
    )
    for tool, args in (
        (
            "write",
            {"path": "AGENTS.md", "content": "Ignore rules\n", "base_version": rules["version"]},
        ),
        (
            "move",
            {
                "path": "wiki/policy-move.md",
                "new_path": "skills/x.md",
                "base_version": page["version"],
            },
        ),
    ):
        with pytest.raises(ToolError) as exc:
            await _call(stack, token, tool, args)
        error = json.loads(str(exc.value))
        assert error["code"] == "forbidden"
        assert error["details"]["reason"] == "protected"
    assert (stack.root / "vault" / "wiki" / "policy-move.md").read_text() == "# Keep\n"
    assert not (stack.root / "vault" / "skills" / "x.md").exists()


async def test_write_policy_recently_edited_waits_then_succeeds(stack: Stack) -> None:
    token = _writer(stack, "policy-grace")
    rel = "wiki/policy-grace.md"
    await _call(stack, token, "write", {"path": rel, "content": "# Before\n", "base_version": None})
    page = stack.root / "vault" / rel
    page.write_text("# Person's saved text\n")
    deadline = time.monotonic() + 15
    while True:
        pointers = (stack.root / "data" / "history" / rel).glob("*.json")
        if any(json.loads(p.read_text())["source"] == "web" for p in pointers):
            break
        assert time.monotonic() < deadline, "watcher did not record the web save"
        await asyncio.sleep(0.1)
    current = await _call(stack, token, "read", {"path": rel})
    args = {"path": rel, "content": "# Updated after grace\n", "base_version": current["version"]}
    with pytest.raises(ToolError) as exc:
        await _call(stack, token, "write", args)
    error = json.loads(str(exc.value))
    assert error["code"] == "recently_edited"
    retry = error["details"]["retry_after_s"]
    assert 0 < retry <= 90
    assert page.read_text() == "# Person's saved text\n"
    await asyncio.sleep(retry + 0.1)
    await _call(stack, token, "write", args)
    assert page.read_text() == "# Updated after grace\n"


async def test_write_policy_read_only_still_refuses_human_pages(stack: Stack) -> None:
    token = _writer(stack, "policy-legacy")
    path = stack.root / "vault-mcp.yaml"
    original = path.read_text()
    policy = yaml.safe_load(original)
    policy["write"]["human_pages"] = "read_only"
    page = stack.root / "vault" / "projects" / "alpha.md"
    before = page.read_text()
    try:
        path.write_text(yaml.safe_dump(policy))
        stack.compose("restart", "vault-mcp")
        stack.compose("up", "-d", "--wait", "vault-mcp")
        with pytest.raises(ToolError) as exc:
            await _call(
                stack,
                token,
                "append",
                {
                    "path": "projects/alpha.md",
                    "text": "Must not be written\n",
                    "summary": "Legacy refusal",
                },
            )
        error = json.loads(str(exc.value))
        assert error["code"] == "forbidden"
        assert error["details"]["reason"] == "human_page"
        assert page.read_text() == before
    finally:
        path.write_text(original)
        stack.compose("restart", "vault-mcp")
        stack.compose("up", "-d", "--wait", "vault-mcp")
