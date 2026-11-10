"""SPEC §16.19 (submitted source → claimed, processed, completed, logged) and §16.20 (budget
reached → job skipped + alert), with a scripted AGENT_COMMAND that follows the D30 contract."""

import json
import time
from pathlib import Path

import pytest
from fastmcp import Client
from harness import REPO, Stack

pytestmark = pytest.mark.profile_local


def run_job(stack: Stack, token: str, budget: str = "") -> tuple[int, str]:
    r = stack.compose(
        "run", "--rm", "--no-deps", "-T",
        "-v", f"{REPO / 'e2e' / 'stub'}:/stub:ro",
        "-e", f"VAULT_MCP_TOKEN={token}", "-e", "AGENT_COMMAND=node /stub/agent.mjs",
        "-e", f"AGENT_MONTHLY_BUDGET={budget}", "-e", "ALERT_WEBHOOK=http://webhook-stub:8099/hook",
        "-e", "GIT_REMOTE=/nowhere",
        "agent-runner", "run-job", "ingest", check=False,
    )  # fmt: skip
    return r.returncode, r.stdout + r.stderr


def test_16_19_and_16_20_agent_runner(stack: Stack) -> None:
    (stack.root / "vault" / "skills").mkdir(exist_ok=True)
    token = stack.token(
        "create",
        "--name",
        "agent-runner",
        "--owner",
        "agent-runner@example.com",
        "--scope",
        "write",
        "--expires",
        "never",
    )
    writer = stack.token(
        "create", "--name", "submitter", "--owner", "s@example.com", "--scope", "write"
    )

    async def submit() -> str:
        async with Client(f"{stack.mcp_url}/mcp", auth=writer) as c:
            out = (
                await c.call_tool(
                    "submit_source",
                    {"title": "Runner test", "content": "# Runner\n\nfacts\n", "origin": "e2e"},
                )
            ).structured_content
            assert out is not None
            return str(out["path"])

    import asyncio

    raw_path = asyncio.run(submit())
    status, output = run_job(stack, token)
    assert status == 0, output
    name = Path(raw_path).name
    vault, data = stack.root / "vault", stack.root / "data"
    assert (vault / "raw" / "_processed" / name).exists()
    assert (vault / "wiki" / "sources" / name).exists()
    audit = [json.loads(x) for x in (data / "audit.log").read_text().splitlines()]
    tools = [
        r["tool"]
        for r in audit
        if r.get("credential_name") == "agent-runner" and r["result"] == "ok"
    ]
    assert {"claim_source", "write", "complete_source"} <= set(tools)
    month = time.strftime("%Y-%m", time.gmtime())
    assert json.loads((data / "agent-runner" / "spend.json").read_text())[month] == 0.5
    requests = list((data / "git-sync" / "requests").glob("*-ingest.json"))
    assert [json.loads(r.read_text())["summary"] for r in requests] == [f"ingested {name}"]
    for r in requests:  # the stack is shared: don't leave a request for the git-sync test
        r.unlink()

    # §16.20: the month's spend (0.5) reaches a 0.4 budget: the next job is skipped and alerted.
    asyncio.run(submit())
    status, output = run_job(stack, token, budget="0.4")
    assert status == 0
    assert "monthly budget reached" in output
    assert len(list((vault / "raw" / "_processed").iterdir())) == 1  # nothing new ingested
    alerts = stack.root / "alerts" / "alerts.jsonl"
    deadline = time.monotonic() + 15
    while not (alerts.exists() and "agent_budget_exceeded" in alerts.read_text()):
        assert time.monotonic() < deadline, "no budget alert"
        time.sleep(0.3)
