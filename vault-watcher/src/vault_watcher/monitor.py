"""Ops monitor (SPEC §12, plan D26): peers' /health over the compose network, heartbeat file
age, and disk usage of /vault and /data. Alerts after 3 consecutive failures, once per outage,
and says when a check recovers. No Docker socket: nothing here can control containers.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)
FAILURES_TO_ALERT = 3
DISK_THRESHOLD = 0.80


class AlertSink(Protocol):
    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool: ...


Probe = Callable[[], str | None]  # None when healthy, else a short reason


@dataclass(frozen=True)
class Check:
    name: str
    event: str  # alert event: service_unhealthy, disk_usage_high
    probe: Probe


def http_probe(url: str, timeout_s: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - fixed http URLs from config
            return None if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return f"unreachable ({type(exc).__name__})"


def heartbeat_probe(path: Path, max_age_s: float) -> str | None:
    """A service that is enabled (its state directory exists) must have touched its heartbeat
    file within `max_age_s`."""
    if not path.parent.is_dir():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return "no heartbeat yet"
    return None if age <= max_age_s else f"heartbeat {int(age)} s old"


def disk_probe(path: Path, usage: Callable[[Path], Any] = shutil.disk_usage) -> str | None:
    u = usage(path)
    used = u.used / u.total if u.total else 0.0
    return None if used <= DISK_THRESHOLD else f"{path} is {used:.0%} full"


class Monitor:
    def __init__(self, checks: Iterable[Check], alerts: AlertSink) -> None:
        self.checks = list(checks)
        self.alerts = alerts
        self._failures: dict[str, int] = {}
        self._alerted: set[str] = set()
        self._outages: dict[
            str, int
        ] = {}  # numbered so the sender's dedupe can't hide a new outage
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        for check in self.checks:
            try:
                reason = check.probe()
            except Exception as exc:  # a crashing probe is a failed check
                reason = f"check failed ({type(exc).__name__})"
            if reason is None:
                self._failures[check.name] = 0
                if check.name in self._alerted:
                    self._alerted.discard(check.name)
                    self.alerts.send(
                        "service_recovered", "info", f"{check.name} is healthy again.", {"check": check.name},
                        subject=f"{check.name}:outage-{self._outages.get(check.name, 0)}",
                    )  # fmt: skip
                continue
            count = self._failures.get(check.name, 0) + 1
            self._failures[check.name] = count
            log.warning("monitor: %s failed (%d): %s", check.name, count, reason)
            if count >= FAILURES_TO_ALERT and check.name not in self._alerted:
                self._alerted.add(check.name)
                self._outages[check.name] = self._outages.get(check.name, 0) + 1
                self.alerts.send(
                    check.event, "high", f"{check.name}: {reason} ({count} checks in a row).",
                    {"check": check.name, "reason": reason}, subject=f"{check.name}:outage-{self._outages.get(check.name, 0)}",
                )  # fmt: skip

    def start(self, interval_s: float) -> None:
        def loop() -> None:
            while not self._stop.wait(interval_s):
                self.run_once()

        self._thread = threading.Thread(target=loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


def ignis_server_plugin_check(data_dir: Path, allowed: str) -> Check:
    """Alert when someone switches on an Ignis server plugin that isn't allowed (Headless Sync can
    copy the whole vault to a personal Obsidian Sync account). ignis-init turns them off again at
    the next start."""
    allowed_ids = {p.strip() for p in allowed.split(",") if p.strip()}

    def probe() -> str | None:
        from vault_watcher.ignis_harden import enabled_server_plugins

        unexpected = [p for p in enabled_server_plugins(data_dir) if p not in allowed_ids]
        return f"Ignis server plugin(s) enabled: {', '.join(unexpected)}" if unexpected else None

    return Check("ignis-server-plugins", "ignis_server_plugin_enabled", probe)


def default_checks(
    peers: dict[str, str],
    heartbeats: dict[str, Path],
    disks: Iterable[Path],
    heartbeat_max_age_s: float,
) -> list[Check]:
    checks = [
        Check(name, "service_unhealthy", partial(http_probe, url)) for name, url in peers.items()
    ]
    checks += [
        Check(name, "service_unhealthy", partial(heartbeat_probe, path, heartbeat_max_age_s))
        for name, path in heartbeats.items()
    ]
    checks += [Check(f"disk {d}", "disk_usage_high", partial(disk_probe, d)) for d in disks]
    return checks
