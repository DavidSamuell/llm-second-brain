"""Everything vault-watcher writes to history. Every method expects the vault lock to be held
by the caller (plan D6/D21): that is what makes a `.prepared` spool entry an orphan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vault_core.audit import AuditLog
from vault_core.fsio import is_temp_name, version_of
from vault_core.history import HistoryStore, Source, Spool, SpoolEntry, classify

log = logging.getLogger(__name__)


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


def is_tracked(rel: str) -> bool:
    """D22: what history records. Git internals, Obsidian's trash, per-user workspace layout,
    caches, and vault-mcp temp files are not content."""
    parts = rel.split("/")
    if parts[0] in (".git", ".trash"):
        return False
    if (
        parts[0] == ".obsidian"
        and len(parts) > 1
        and (parts[1].startswith("workspace") or parts[1] == "cache")
    ):
        return False
    return not is_temp_name(parts[-1])


@dataclass
class Recorder:
    vault_dir: Path
    data_dir: Path
    audit: AuditLog
    alerts: AlertSink
    history: HistoryStore = field(init=False)
    spool: Spool = field(init=False)
    _scan_cache: dict[str, tuple[int, int]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.history = HistoryStore(self.data_dir)
        self.spool = Spool(self.data_dir)

    # -- spool -----------------------------------------------------------------------------

    def drain(self) -> int:
        """Record committed transactions and resolve orphaned prepared ones. Returns the count."""
        entries = self.spool.entries()
        for entry in entries:
            if entry.state == "committed":
                self.history.record_entry(entry)
            else:
                self._recover(entry)
            self.spool.ack(entry)
        return len(entries)

    def _recover(self, entry: SpoolEntry) -> None:
        target_rel = entry.new_path or entry.path
        target = self.vault_dir / target_rel
        for leftover in target.parent.glob(f".{target.name}.vault-mcp-*.tmp"):
            leftover.unlink(missing_ok=True)
        current = version_of(target.read_bytes()) if target.is_file() else None
        outcome = classify(
            current,
            pre_sha256=entry.pre_sha256,
            post_sha256=entry.post_sha256,
            pre_known=entry.pre_known,
        )
        fields = {
            "path": entry.path,
            "new_path": entry.new_path,
            "credential_id": entry.credential_id,
        }
        if outcome == "applied":
            self.history.record_entry(entry)
            if not self.audit.has_id(entry.audit_id):
                self.audit.append(
                    id=entry.audit_id, service="vault-watcher", tool=entry.tool, result="ok",
                    recovered=True, new_version=entry.post_sha256, **fields,
                )  # fmt: skip
        elif outcome == "aborted":
            self.history.record_entry(entry, include_post=False)
            self.audit.append(
                id=entry.audit_id,
                service="vault-watcher",
                tool=entry.tool,
                result="aborted",
                **fields,
            )
        else:
            self.history.record_entry(entry, unconfirmed=True)
            self.audit.append(
                id=entry.audit_id,
                service="vault-watcher",
                tool=entry.tool,
                result="unknown",
                **fields,
            )
            self.alerts.send(
                "history_unconfirmed",
                "high",
                f"Could not confirm whether an interrupted MCP write to {target_rel} landed; "
                "its content is kept in history as unconfirmed.",
                {"path": target_rel, "audit_id": entry.audit_id},
                subject=entry.audit_id,
            )
        log.warning(
            "recovered interrupted MCP write %s on %s: %s", entry.audit_id, target_rel, outcome
        )

    # -- vault changes -----------------------------------------------------------------------

    def snapshot(self, rel: str, source: Source) -> bool:
        """Record the file's current state if it differs from its newest version."""
        if not is_tracked(rel):
            return False
        path = self.vault_dir / rel
        if path.exists() and not path.resolve().is_relative_to(self.vault_dir.resolve()):
            return False  # a symlink out of the vault: never copy outside files into history
        head = self.history.head(rel)
        if not path.is_file():
            if head is not None and head.sha256 is not None:
                self.history.record(rel, None, event="delete", source=source)
                self._scan_cache.pop(rel, None)
                return True
            return False
        data = path.read_bytes()
        if head is not None and head.sha256 == version_of(data):
            return False
        self.history.record(
            rel,
            data,
            event="create" if head is None or head.sha256 is None else "modify",
            source=source,
        )
        stat = path.stat()
        self._scan_cache[rel] = (stat.st_mtime_ns, stat.st_size)
        return True

    def scan(self) -> int:
        """Full scan (startup and periodic, D22): anything that changed without an event —
        including content saved after a vault-mcp crash — is recorded as `unknown`."""
        changed = 0
        seen: set[str] = set()
        for path in self.vault_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.vault_dir).as_posix()
            if not is_tracked(rel):
                continue
            seen.add(rel)
            stat = path.stat()
            if self._scan_cache.get(rel) == (stat.st_mtime_ns, stat.st_size):
                continue
            if self.snapshot(rel, "unknown"):
                changed += 1
            self._scan_cache[rel] = (stat.st_mtime_ns, stat.st_size)
        for rel in self.history.paths():
            if rel not in seen and is_tracked(rel) and self.snapshot(rel, "unknown"):
                changed += 1
        return changed
