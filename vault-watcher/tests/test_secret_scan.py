"""Watcher secret scan (SPEC §7.3, §16.13 Ignis half; plan D15, D24, D25)."""

import json
from pathlib import Path
from typing import Any

import pytest

from vault_core.audit import AuditLog
from vault_core.history import HistoryStore
from vault_core.review_queue import QUEUE_PATH
from vault_watcher.recorder import Recorder

FAKE_KEY = "AKIA" + "Q3RTZ7XK2P4LMNBV"


class Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict[str, Any]]] = []

    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool:
        self.sent.append((event, severity, message, details or {}))
        return True


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Recorder, Alerts, Path, Path]:
    vault, data = tmp_path / "vault", tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    data.mkdir()
    alerts = Alerts()
    return Recorder(vault, data, AuditLog(data / "audit.log"), alerts), alerts, vault, data


def everything_written(vault: Path, data: Path, alerts: Alerts) -> str:
    """Every byte the kit produced outside the offending file itself."""
    parts = [json.dumps(alerts.sent)]
    parts += [
        p.read_text(errors="replace")
        for p in data.rglob("*")
        if p.is_file() and "history" not in p.parts
    ]
    parts += [p.read_text(errors="replace") for p in (vault / "_system").rglob("*") if p.is_file()]
    return "\n".join(parts)


def test_a_secret_saved_in_the_browser_alerts_once_and_adds_one_masked_review_item(
    setup: tuple[Recorder, Alerts, Path, Path],
) -> None:
    rec, alerts, vault, data = setup
    page = vault / "wiki" / "notes.md"
    original = f"# Notes\n\naws: {FAKE_KEY}\n"
    page.write_text(original)
    rec.snapshot("wiki/notes.md", "web")
    page.write_text(original + "more text\n")  # same secret still there: no repeat
    rec.snapshot("wiki/notes.md", "web")

    secret_alerts = [a for a in alerts.sent if a[0] == "secret_detected"]
    assert len(secret_alerts) == 1
    assert secret_alerts[0][1] == "high"
    queue = (vault / QUEUE_PATH).read_text()
    items = [line for line in queue.splitlines() if line.startswith("- [ ] ")]
    assert len(items) == 1
    assert "#review/high" in items[0]
    assert "[[wiki/notes]]" in items[0]
    assert "line 3" in items[0]
    rows = [json.loads(x) for x in (data / "audit.log").read_text().splitlines()]
    assert [r["result"] for r in rows if r["tool"] == "secret_scan"] == ["secret_detected"]
    assert FAKE_KEY not in everything_written(vault, data, alerts)
    assert page.read_text() == original + "more text\n"  # human content untouched


def test_the_review_queue_write_is_recorded_and_does_not_retrigger(
    setup: tuple[Recorder, Alerts, Path, Path],
) -> None:
    rec, alerts, vault, data = setup
    (vault / "wiki" / "a.md").write_text(f"k={FAKE_KEY}\n")
    rec.snapshot("wiki/a.md", "web")
    assert rec.scan() == 0  # the queue file was already recorded when it was written
    assert HistoryStore(data).head(QUEUE_PATH) is not None
    assert len([a for a in alerts.sent if a[0] == "secret_detected"]) == 1


def test_a_second_location_or_new_secret_is_a_new_finding(
    setup: tuple[Recorder, Alerts, Path, Path],
) -> None:
    rec, alerts, vault, _ = setup
    (vault / "wiki" / "a.md").write_text(f"k={FAKE_KEY}\n")
    rec.snapshot("wiki/a.md", "web")
    (vault / "wiki" / "b.md").write_text(f"k={FAKE_KEY}\n")
    rec.snapshot("wiki/b.md", "web")
    assert len([a for a in alerts.sent if a[0] == "secret_detected"]) == 2


def test_dedupe_survives_a_restart(setup: tuple[Recorder, Alerts, Path, Path]) -> None:
    rec, alerts, vault, data = setup
    (vault / "wiki" / "a.md").write_text(f"k={FAKE_KEY}\n")
    rec.snapshot("wiki/a.md", "web")
    again = Recorder(vault, data, AuditLog(data / "audit.log"), alerts)
    (vault / "wiki" / "a.md").write_text(f"k={FAKE_KEY}\nedit\n")
    again.snapshot("wiki/a.md", "web")
    assert len([a for a in alerts.sent if a[0] == "secret_detected"]) == 1


def test_clean_files_and_binary_attachments_do_not_alert(
    setup: tuple[Recorder, Alerts, Path, Path],
) -> None:
    rec, alerts, vault, _ = setup
    (vault / "wiki" / "clean.md").write_text("# Clean\n")
    (vault / "wiki" / "image.png").write_bytes(b"\x89PNG" + FAKE_KEY.encode())
    rec.snapshot("wiki/clean.md", "web")
    rec.snapshot("wiki/image.png", "web")
    assert alerts.sent == []
