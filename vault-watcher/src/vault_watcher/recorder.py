"""Everything vault-watcher writes to history. Every method expects the vault lock to be held
by the caller (plan D6/D21): that is what makes a `.prepared` spool entry an orphan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vault_core.audit import AuditLog
from vault_core.fsio import is_temp_name, version_of
from vault_core.history import HistoryStore, Outcome, Source, Spool, SpoolEntry, classify
from vault_core.paths import through_symlink
from vault_core.search_index import SearchIndex

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
    index: SearchIndex | None = None
    history: HistoryStore = field(init=False)
    spool: Spool = field(init=False)
    _scan_cache: dict[str, tuple[int, int]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.history = HistoryStore(self.data_dir)
        self.spool = Spool(self.data_dir)

    # -- spool -----------------------------------------------------------------------------

    def drain(self) -> int:
        """Record committed transactions and resolve orphaned prepared ones. Returns the count.

        Three passes, so a crash part-way can't lose a group's outcome: (1) record/recover
        every entry, (2) alert on every partly applied move group, (3) only then acknowledge.
        Recovery and recording are idempotent, so a crash before (3) just repeats them."""
        entries = self.spool.entries()
        groups: dict[str, set[Outcome]] = {}
        for entry in entries:
            if entry.state == "committed":
                self.history.record_entry(entry)
                outcome: Outcome = "applied"
            else:
                outcome = self._recover(entry)
            if entry.group_id:
                groups.setdefault(entry.group_id, set()).add(outcome)
            self._reindex(entry.new_path or entry.path)
            if entry.new_path:
                self._reindex(entry.path)
        for group_id, outcomes in groups.items():
            if len(outcomes) > 1 or "unconfirmed" in outcomes:
                # D12: a move rewrites several files; an interrupted one may be half applied.
                self.alerts.send(
                    "move_partial", "high",
                    "An interrupted move was only partly applied; check its links and files.",
                    {"group_id": group_id, "outcomes": sorted(outcomes)}, subject=group_id,
                )  # fmt: skip
        for entry in entries:
            self.spool.ack(entry)
        return len(entries)

    def _classify(self, entry: SpoolEntry) -> Outcome:
        target = self.vault_dir / (entry.new_path or entry.path)
        current = version_of(target.read_bytes()) if target.is_file() else None
        if entry.new_path is None:
            return classify(
                current,
                pre_sha256=entry.pre_sha256,
                post_sha256=entry.post_sha256,
                pre_known=entry.pre_known,
            )
        # A move: the new path decides. If the new file landed, finish the move by removing
        # the old path when it still holds exactly the pre-move content.
        source = self.vault_dir / entry.path
        if current == entry.post_sha256:
            if (
                source.is_file()
                and entry.pre_known
                and version_of(source.read_bytes()) == entry.pre_sha256
            ):
                source.unlink()
            return "applied"
        if (
            current is None
            and source.is_file()
            and version_of(source.read_bytes()) == entry.pre_sha256
        ):
            return "aborted"
        return "unconfirmed"

    def _recover(self, entry: SpoolEntry) -> Outcome:
        target_rel = entry.new_path or entry.path
        target = self.vault_dir / target_rel
        for leftover in target.parent.glob(f".{target.name}.vault-mcp-*.tmp"):
            leftover.unlink(missing_ok=True)
        outcome = self._classify(entry)
        fields: dict[str, Any] = {
            "path": entry.path,
            "new_path": entry.new_path,
            "credential_id": entry.credential_id,
        }
        # Every branch is idempotent: the watcher can die after writing the audit line and
        # before acknowledging the entry, and the next drain must not repeat the line.
        logged = self.audit.has_id(entry.audit_id)
        if outcome == "applied":
            self.history.record_entry(entry)
            if not logged:
                self.audit.append(
                    id=entry.audit_id, service="vault-watcher", tool=entry.tool, result="ok",
                    recovered=True, new_version=entry.post_sha256, **fields,
                )  # fmt: skip
        elif outcome == "aborted":
            self.history.record_entry(entry, include_post=False)
            if not logged:
                self.audit.append(
                    id=entry.audit_id,
                    service="vault-watcher",
                    tool=entry.tool,
                    result="aborted",
                    **fields,
                )
        else:
            self.history.record_entry(entry, unconfirmed=True)
            if not logged:
                # Alert before the audit line: a crash in between repeats the alert (at least
                # once) rather than losing it.
                self.alerts.send(
                    "history_unconfirmed",
                    "high",
                    f"Could not confirm whether an interrupted MCP write to {target_rel} landed; "
                    "its content is kept in history as unconfirmed.",
                    {"path": target_rel, "audit_id": entry.audit_id},
                    subject=entry.audit_id,
                )
                self.audit.append(
                    id=entry.audit_id,
                    service="vault-watcher",
                    tool=entry.tool,
                    result="unknown",
                    **fields,
                )
        log.warning(
            "recovered interrupted MCP write %s on %s: %s", entry.audit_id, target_rel, outcome
        )
        return outcome

    def _reindex(self, rel: str) -> None:
        """Bring the search index in line with the file on disk (D23)."""
        if self.index is None:
            return
        path = self.vault_dir / rel
        inside = path.is_file() and not through_symlink(self.vault_dir, rel)
        self.index.update(rel, path.read_bytes() if inside else None)

    # -- vault changes -----------------------------------------------------------------------

    def snapshot(self, rel: str, source: Source) -> bool:
        """Record the file's current state if it differs from its newest version."""
        if not is_tracked(rel):
            return False
        path = self.vault_dir / rel
        if path.exists() and through_symlink(self.vault_dir, rel):
            return (
                False  # symlinks are never followed: no outside files, no policy-bypassing aliases
            )
        head = self.history.head(rel)
        if not path.is_file():
            if head is not None and head.sha256 is not None:
                self.history.record(rel, None, event="delete", source=source)
                self._scan_cache.pop(rel, None)
                self._reindex(rel)
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
        if self.index is not None:
            self.index.update(rel, data)
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
