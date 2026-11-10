"""Secret scan of every recorded change (SPEC §7.3, plan D15/D24/D25).

A finding (path, rule, fingerprint) is reported once, ever: an alert, a masked `#review/high`
item in the review queue, and an audit line. Nothing reveals the secret: rule id and line
number only. Human content is never edited. Runs inside the watcher's locked section.
"""

from __future__ import annotations

import secrets as token_gen
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from vault_core import review_queue
from vault_core.audit import AuditLog, new_id
from vault_core.fsio import replace_from_temp, write_temp_sibling
from vault_core.secrets import scan_text


def _as_text(data: bytes) -> str | None:
    """Any changed file that is text, whatever its name (`.env`, `.toml`, scripts, no
    extension). Binary: a NUL byte in the first 8 KiB, or not valid UTF-8."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


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


class SecretScanner:
    def __init__(self, vault_dir: Path, db_path: Path, audit: AuditLog, alerts: AlertSink) -> None:
        self.vault_dir = vault_dir
        self.db_path = db_path
        self.audit = audit
        self.alerts = alerts
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secret_findings ("
                " path TEXT NOT NULL, rule TEXT NOT NULL, fingerprint TEXT NOT NULL, first_seen TEXT NOT NULL,"
                " PRIMARY KEY (path, rule, fingerprint))"
            )
        conn.close()

    def _known(self, path: str, rule: str, fingerprint: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM secret_findings WHERE path = ? AND rule = ? AND fingerprint = ?",
                (path, rule, fingerprint),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def _remember(self, path: str, rule: str, fingerprint: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO secret_findings VALUES (?, ?, ?, ?)",
                    (path, rule, fingerprint, datetime.now(UTC).isoformat()),
                )
        finally:
            conn.close()

    def check(self, rel: str, data: bytes, record_queue: Callable[[str], object]) -> int:
        """Report new findings in `rel`. `record_queue` records the review queue's new version
        in history. Returns the number of new findings."""
        if rel == review_queue.QUEUE_PATH:
            return 0
        text = _as_text(data)
        if text is None:
            return 0
        new = [f for f in scan_text(text) if not self._known(rel, f.rule, f.fingerprint)]
        for finding in new:
            # At least once: alert, queue item, audit, then remember. A crash part-way repeats.
            self.alerts.send(
                "secret_detected", "high",
                f"Possible secret in {rel} ({finding.masked()}). Remove it and rotate the credential.",
                {"path": rel, "rule": finding.rule, "line": finding.line},
                subject=f"{rel}:{finding.rule}:{finding.fingerprint}",
            )  # fmt: skip
            self._add_review_item(rel, finding.masked())
            record_queue(review_queue.QUEUE_PATH)
            self.audit.append(
                service="vault-watcher", tool="secret_scan", result="secret_detected",
                path=rel, rule=finding.rule, line=finding.line,
            )  # fmt: skip
            self._remember(rel, finding.rule, finding.fingerprint)
        return len(new)

    def _add_review_item(self, rel: str, masked: str) -> None:
        item = review_queue.format_item(
            rel, f"Possible secret ({masked})",
            "Remove it from the page and rotate the credential; old versions stay in history until purged (scripts/history)",
            by="vault-watcher", owner="system", ts=datetime.now(UTC), item_id=new_id(), priority="high",
        )  # fmt: skip
        queue = self.vault_dir / review_queue.QUEUE_PATH
        current = queue.read_text(encoding="utf-8", errors="replace") if queue.is_file() else None
        queue.parent.mkdir(parents=True, exist_ok=True)
        tmp = write_temp_sibling(
            queue, review_queue.add_item(current, item).encode(), tag=token_gen.token_hex(4)
        )
        replace_from_temp(tmp, queue)
