import json
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from vault_core.policy import RateLimits
from vault_mcp.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


class Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool:
        self.sent.append((event, subject))
        return True


def run_now(send: Any) -> None:
    send()


def limiter(clock: Clock, alerts: Alerts, **limits: int) -> RateLimiter:
    base = {"requests_per_minute": 120, "writes_per_minute": 30, "writes_per_day": 2000}
    return RateLimiter(
        RateLimits(**(base | limits)),
        alerts=alerts,
        clock=clock,
        dispatch=run_now,
    )


def code_and_retry(exc: pytest.ExceptionInfo[ToolError]) -> tuple[str, float]:
    e = json.loads(str(exc.value))
    return e["code"], e["details"]["retry_after_s"]


def test_requests_per_minute(clock: Clock) -> None:
    rl = limiter(clock, Alerts(), requests_per_minute=3)
    for _ in range(3):
        rl.check("c1", write=False)
    with pytest.raises(ToolError) as exc:
        rl.check("c1", write=False)
    assert code_and_retry(exc) == ("rate_limited", 60.0)
    rl.check("c2", write=False)  # per credential
    clock.t += 60
    rl.check("c1", write=False)


def test_writes_count_against_both_write_and_request_limits(clock: Clock) -> None:
    rl = limiter(clock, Alerts(), requests_per_minute=10, writes_per_minute=2)
    rl.check("c", write=True)
    rl.check("c", write=True)
    with pytest.raises(ToolError):
        rl.check("c", write=True)
    for _ in range(7):  # 2 writes + 7 reads = 9 accepted requests
        rl.check("c", write=False)
    rl.check("c", write=False)
    with pytest.raises(ToolError):
        rl.check("c", write=False)


def test_writes_per_day_and_retry_after_points_at_the_oldest_write(clock: Clock) -> None:
    rl = limiter(clock, Alerts(), writes_per_day=2)
    rl.check("c", write=True)
    clock.t += 3600
    rl.check("c", write=True)
    clock.t += 60
    with pytest.raises(ToolError) as exc:
        rl.check("c", write=True)
    assert code_and_retry(exc) == ("rate_limited", 86400.0 - 3660)


def test_rejected_calls_do_not_extend_the_window(clock: Clock) -> None:
    rl = limiter(clock, Alerts(), requests_per_minute=1)
    rl.check("c", write=False)
    for _ in range(5):
        clock.t += 10
        with pytest.raises(ToolError):
            rl.check("c", write=False)
    clock.t += 11
    rl.check("c", write=False)


def test_repeated_hits_alert_once_per_credential(clock: Clock) -> None:
    alerts = Alerts()
    rl = limiter(clock, alerts, requests_per_minute=1)
    rl.check("c", write=False)
    for _ in range(4):
        with pytest.raises(ToolError):
            rl.check("c", write=False)
    assert alerts.sent == []
    for _ in range(10):
        with pytest.raises(ToolError):
            rl.check("c", write=False)
    assert alerts.sent == [("rate_limit_repeated", "c")]


def test_hits_spread_beyond_ten_minutes_do_not_alert(clock: Clock) -> None:
    alerts = Alerts()
    rl = limiter(clock, alerts, requests_per_minute=1)
    for _ in range(6):
        rl.check("c", write=False)
        with pytest.raises(ToolError):
            rl.check("c", write=False)
        clock.t += 150  # 6 hits over 12.5 minutes: never 5 within 10
    assert alerts.sent == []


async def test_16_8_server_returns_rate_limited_over_mcp(vault, data, tmp_path, store) -> None:  # type: ignore[no-untyped-def]
    from fastmcp import Client

    from vault_core.config import Settings
    from vault_mcp.server import build_server

    policy_file = tmp_path / "tight.yaml"
    policy_file.write_text("write: {allow: [wiki/]}\nrate_limits: {requests_per_minute: 2}\n")
    token, _ = store.create_bearer(name="burst", owner="b@example.com", scope="read")
    async with Client(
        build_server(
            Settings(vault_dir=vault, data_dir=data, policy_file=policy_file), stdio_token=token
        )
    ) as c:
        await c.call_tool("get_schema", {})
        await c.call_tool("get_schema", {})
        with pytest.raises(ToolError) as exc:
            await c.call_tool("get_schema", {})
    assert json.loads(str(exc.value))["code"] == "rate_limited"
    rows = [json.loads(x) for x in (data / "audit.log").read_text().splitlines()]
    assert rows[-1]["result"] == "rate_limited"


def test_alert_delivery_does_not_block_the_caller(clock: Clock) -> None:
    import threading
    import time as time_module

    release = threading.Event()

    class SlowAlerts(Alerts):
        def send(self, *args: Any, **kwargs: Any) -> bool:
            release.wait(5)  # a webhook that hangs
            return super().send(*args, **kwargs)

    rl = RateLimiter(RateLimits(requests_per_minute=1), alerts=SlowAlerts(), clock=clock)
    rl.check("c", write=False)
    start = time_module.monotonic()
    for _ in range(5):
        with pytest.raises(ToolError):
            rl.check("c", write=False)
    assert time_module.monotonic() - start < 1
    release.set()
