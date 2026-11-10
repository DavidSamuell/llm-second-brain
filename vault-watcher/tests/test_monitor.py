"""Ops monitor (SPEC §12, plan D26)."""

import os
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from vault_watcher.monitor import Check, Monitor, disk_probe, heartbeat_probe, http_probe


class Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool:
        self.sent.append((event, subject, details or {}))
        return True


def test_alerts_after_three_consecutive_failures_and_once_per_outage() -> None:
    state = {"ok": False}
    alerts = Alerts()
    monitor = Monitor(
        [Check("vault-mcp", "service_unhealthy", lambda: None if state["ok"] else "down")], alerts
    )
    monitor.run_once()
    monitor.run_once()
    assert alerts.sent == []
    monitor.run_once()
    assert [(e, d["check"]) for e, _, d in alerts.sent] == [("service_unhealthy", "vault-mcp")]
    monitor.run_once()
    assert len(alerts.sent) == 1  # still the same outage
    state["ok"] = True
    monitor.run_once()
    assert alerts.sent[-1][0] == "service_recovered"
    state["ok"] = False
    for _ in range(3):
        monitor.run_once()
    unhealthy = [s for e, s, _ in alerts.sent if e == "service_unhealthy"]
    assert len(unhealthy) == 2
    assert len(set(unhealthy)) == 2  # distinct subjects: a second outage isn't deduped away


def test_a_single_success_resets_the_count() -> None:
    results = iter(["down", "down", None, "down", "down"])
    alerts = Alerts()
    monitor = Monitor([Check("x", "service_unhealthy", lambda: next(results))], alerts)
    for _ in range(5):
        monitor.run_once()
    assert alerts.sent == []


def test_a_crashing_probe_counts_as_a_failure() -> None:
    def boom() -> str | None:
        raise OSError("socket")

    alerts = Alerts()
    monitor = Monitor([Check("x", "service_unhealthy", boom)], alerts)
    for _ in range(3):
        monitor.run_once()
    assert len(alerts.sent) == 1


def test_disk_probe_fails_above_80_percent(tmp_path: Path) -> None:
    def usage(pct: int) -> Any:
        return lambda _: shutil._ntuple_diskusage(100, pct, 100 - pct)  # type: ignore[attr-defined]

    assert disk_probe(tmp_path, usage=usage(80)) is None
    reason = disk_probe(tmp_path, usage=usage(81))
    assert reason is not None
    assert "81" in reason


def test_heartbeat_probe(tmp_path: Path) -> None:
    beat = tmp_path / "git-sync" / "heartbeat"
    beat.parent.mkdir()
    assert heartbeat_probe(beat, max_age_s=60) is not None  # enabled but never beat
    beat.touch()
    assert heartbeat_probe(beat, max_age_s=60) is None
    old = time.time() - 120
    os.utime(beat, (old, old))
    assert heartbeat_probe(beat, max_age_s=60) is not None


@pytest.fixture
def server() -> Any:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 503)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_http_probe(server: Any) -> None:
    port = server.server_address[1]
    assert http_probe(f"http://127.0.0.1:{port}/health") is None
    assert http_probe(f"http://127.0.0.1:{port}/other") is not None
    server.shutdown()
    server.server_close()
    assert http_probe(f"http://127.0.0.1:{port}/health", timeout_s=1) is not None


def test_only_services_the_stack_runs_are_checked() -> None:
    from vault_watcher.monitor import enabled_heartbeats

    configured = {"git-sync": "git-sync/heartbeat", "agent-runner": "agent-runner/heartbeat"}
    assert enabled_heartbeats(configured, "") == {}
    assert enabled_heartbeats(configured, "git") == {"git-sync": "git-sync/heartbeat"}
    assert enabled_heartbeats(configured, "agent, git") == configured
