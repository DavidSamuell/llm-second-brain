"""SPEC §12 / plan D26: unhealthy services alert; nothing mounts the Docker socket."""

import asyncio
import json
import time
from pathlib import Path

import httpx
from harness import REPO, Stack


async def _wait_for(predicate, timeout_s: float, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.5)


def _alerts(stack: Stack) -> list[dict[str, object]]:
    path = stack.root / "alerts" / "alerts.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def test_stopping_vault_mcp_alerts_after_three_failed_checks(stack: Stack) -> None:
    stack.compose("stop", "vault-mcp")
    try:
        await _wait_for(
            lambda: any(
                a.get("event") == "service_unhealthy" and a.get("details", {}).get("check") == "vault-mcp"  # type: ignore[union-attr]
                for a in _alerts(stack)
            ),
            30, "no service_unhealthy alert for vault-mcp",
        )  # fmt: skip
    finally:
        stack.compose("start", "vault-mcp")

    def healthy() -> bool:
        try:
            return httpx.get(f"{stack.mcp_url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    await _wait_for(healthy, 60, "vault-mcp did not come back")
    await _wait_for(
        lambda: any(a.get("event") == "service_recovered" for a in _alerts(stack)),
        30,
        "no recovery notice",
    )


def test_no_service_mounts_the_docker_socket(stack: Stack) -> None:
    config = stack.compose("config", "--format", "json").stdout
    assert "docker.sock" not in config
    for compose_file in [
        REPO / "docker-compose.yml",
        *Path(REPO / "ingress").glob("*/compose.yml"),
    ]:
        assert "docker.sock" not in compose_file.read_text()
