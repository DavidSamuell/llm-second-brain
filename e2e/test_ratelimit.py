"""SPEC §16.8: exceeding a rate limit returns a rate-limit error; repeated hits alert."""

import asyncio
import json
import time

from fastmcp import Client
from fastmcp.exceptions import ToolError
from harness import Stack


async def test_16_8_rate_limit_error_and_alert_on_repeated_hits(stack: Stack) -> None:
    token = stack.token(
        "create", "--name", "flooder", "--owner", "f@example.com", "--scope", "read"
    )
    limited = 0
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as client:
        for _ in range(130):  # default policy: 120 requests per minute
            try:
                await client.call_tool("get_schema", {})
            except ToolError as exc:
                assert json.loads(str(exc))["code"] == "rate_limited"
                limited += 1
    assert limited >= 5
    alerts_file = stack.root / "alerts" / "alerts.jsonl"
    deadline = time.monotonic() + 15
    while not alerts_file.exists():
        assert time.monotonic() < deadline, "no alert reached the webhook"
        await asyncio.sleep(0.2)
    alerts = [json.loads(x) for x in alerts_file.read_text().splitlines()]
    repeated = [a for a in alerts if a.get("event") == "rate_limit_repeated"]
    assert len(repeated) == 1
