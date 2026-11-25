"""Test-only MAINTENANCE_AGENT_COMMAND: a scripted "agent" that does its work through the real
vault-mcp with the MCP config scripts/maintenance hands it, then prints a Claude Code style
result JSON. STUB_MODE picks the behaviour; STUB_OUT receives what the stub observed."""

import asyncio
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError

MODE = os.environ.get("STUB_MODE", "ok")
RUN_ID = os.environ["MAINTENANCE_RUN_ID"]
CONFIG = Path(os.environ["MAINTENANCE_MCP_CONFIG"])
STAGE = os.environ.get("MAINTENANCE_STAGE", "lint")
REPORT = os.environ.get("MAINTENANCE_REPORT", "_system/lint-report.md")
NONCE = "dream_run" if STAGE == "dream" else "maintenance_run"
DREAM_PAGE = "wiki/dream-a.md"


def observe(token: str) -> dict[str, Any]:
    """What the test checks about how the stub was started: config privacy, no token on argv."""
    on_argv = []
    for proc in Path("/proc").iterdir():
        try:
            if token in (proc / "cmdline").read_bytes().decode(errors="replace"):
                on_argv.append(proc.name)
        except OSError:
            continue
    return {
        "config": str(CONFIG),
        "config_mode": stat.S_IMODE(CONFIG.stat().st_mode),
        "config_in_cwd": CONFIG.resolve().is_relative_to(Path.cwd().resolve()),
        "cwd": str(Path.cwd()),
        "cwd_files": sorted(p.relative_to(Path.cwd()).as_posix() for p in Path.cwd().rglob("*")),
        "token_on_argv": on_argv,
        "token_in_env": any(token in v for v in os.environ.values()),
        "dry_run": os.environ.get("MAINTENANCE_DRY_RUN"),
        "prompt": (prompt := Path(os.environ["MAINTENANCE_PROMPT_FILE"]).read_text()),
        "scope": re.findall(r"^- (\S+) \(last changed", prompt, re.MULTILINE),
        "stage": STAGE,
    }


def report(
    errors: tuple[str, ...] = (), unresolved: tuple[str, ...] = (), run_id: str = RUN_ID
) -> str:
    lines = [
        "---", "title: Stub report", "maintainer: agent", f"{NONCE}: {run_id}",
        f"updated: {time.strftime('%Y-%m-%d', time.gmtime())}", "---", "# Stub report",
        "## Outcome", f"errors: {len(errors)}", f"unresolved: {len(unresolved)}",
        "## Counts", "- broken wikilinks: 1 found, 1 fixed", "## Fixes",
        "- wiki/maint-broken.md: fixed [[wiki/missing-page]]", "## Errors",
        *[f"- error: {e}" for e in errors], "## Unresolved", *[f"- unresolved: {u}" for u in unresolved],
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


async def call(c: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return (await c.call_tool(tool, args)).structured_content or {}


async def read_or_none(c: Client, path: str) -> dict[str, Any] | None:
    try:
        return await call(c, "read", {"path": path})
    except ToolError as exc:
        if '"not_found"' in str(exc):
            return None
        raise


async def write_report(c: Client, text: str) -> None:
    current = await read_or_none(c, REPORT)
    await call(c, "write", {"path": REPORT, "content": text,
                            "base_version": current["version"] if current else None})  # fmt: skip


async def main() -> None:
    server = json.loads(CONFIG.read_text())["mcpServers"]["vault"]
    token = server["headers"]["Authorization"].removeprefix("Bearer ")
    seen = observe(token)
    result: dict[str, Any] = {"type": "result", "subtype": "success", "is_error": False,
                              "total_cost_usd": 0.42, "result": "SUMMARY: stub run"}  # fmt: skip

    if MODE == "sleep":
        Path(os.environ["STUB_OUT"]).write_text(json.dumps(seen))
        await asyncio.sleep(30)
    async with Client(server["url"], auth=token) as c:
        await call(c, "get_schema", {})
        if MODE == "dryrun":
            page = await call(c, "read", {"path": "wiki/maint-broken.md"})
            try:
                await call(c, "write", {"path": "wiki/maint-broken.md", "content": "changed\n",
                                        "base_version": page["version"]})  # fmt: skip
                seen["write"] = "ok"
            except ToolError as exc:
                seen["write"] = json.loads(str(exc))["code"]
            result["result"] = "# Preview\n\nWould fix 1 broken wikilink in wiki/maint-broken.md.\n"
        elif MODE == "dream":  # corrects one page in scope against its source, then reports
            page = await call(c, "read", {"path": DREAM_PAGE})
            await call(c, "write", {"path": DREAM_PAGE,
                                    "content": page["content"].replace("runs hourly", "runs nightly"),
                                    "base_version": page["version"]})  # fmt: skip
            await write_report(c, report())
            await call(c, "append", {"path": "_system/log.md",
                                     "text": f"- dream {RUN_ID} — 1 claim corrected (by maintenance-dream)\n"})  # fmt: skip
        elif MODE in ("ok", "unresolved"):
            page = await call(c, "read", {"path": "wiki/maint-broken.md"})
            await call(c, "write", {"path": "wiki/maint-broken.md",
                                    "content": page["content"].replace("[[wiki/missing-page]]", "[[wiki/index]]"),
                                    "base_version": page["version"]})  # fmt: skip
            index = await call(c, "read", {"path": "_system/index.md"})
            await call(c, "write", {"path": "_system/index.md",
                                    "content": index["content"] + "- [[wiki/maint-broken]]\n",
                                    "base_version": index["version"]})  # fmt: skip
            unresolved = (
                ("wiki/maint-dupe.md: duplicate of wiki/index.md",) if MODE == "unresolved" else ()
            )
            await write_report(c, report(unresolved=unresolved))
            await call(c, "append", {"path": "_system/log.md",
                                     "text": f"- maintenance {RUN_ID} — 1 fix (by maintenance)\n"})  # fmt: skip
        elif MODE == "errorline":
            await write_report(c, report(errors=("graph_lint: tool kept failing",)))
        elif MODE == "stale":  # rewrites the report, but not as this run
            await write_report(c, report(run_id="20000101T000000Z-00000000"))
        elif MODE == "budget":  # what Claude Code prints when --max-budget-usd stops it
            result.update(subtype="error_max_budget_usd", is_error=True, total_cost_usd=5.01)
            del result["result"]
            await write_report(c, report())
            seen["exit"] = 1
        elif MODE == "fail":
            seen["exit"] = 1
        elif MODE == "malformed":
            result["is_error"] = "false"
            await write_report(c, report())
        elif MODE == "errorcount":
            await write_report(c, report().replace("errors: 0", "errors: 18446744073709551616"))
        # "noreport": does nothing; "garbage": prints no result JSON.

    Path(os.environ["STUB_OUT"]).write_text(json.dumps(seen))
    if MODE == "garbage":
        print("I fixed everything, trust me.")
    else:
        print(json.dumps(result))
    if MODE == "fail":
        print(f"stub failure; token {token}", file=sys.stderr)  # must be redacted from the log
    sys.exit(seen.get("exit", 0))


assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", RUN_ID), RUN_ID
asyncio.run(main())
