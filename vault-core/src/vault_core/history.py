"""Version history under `<data>/history` and the D21 MCP→watcher spool (plan D21).

- Blobs: `.objects/<sha[:2]>/<sha>`, gzip, content-addressed (identical content stored once).
- Versions: one pointer file per version at `<vault-path>/<YYYYMMDDTHHMMSS.ffffffZ>.json`.
  A delete is a pointer with `sha256: null` (tombstone).
- Spool: `.inbox/<audit_id>.prepared/` → renamed to `.committed/` at the commit point.
  vault-mcp writes the spool; vault-watcher is the only writer of pointers and blobs.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from vault_core.fsio import fsync_dir, version_of

Event = Literal["create", "modify", "rename", "delete"]
Source = Literal["mcp", "web", "git", "unknown"]
Outcome = Literal["applied", "aborted", "unconfirmed"]
_TS_FORMAT = "%Y%m%dT%H%M%S.%fZ"


def format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_TS_FORMAT)


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts.split("_")[0], _TS_FORMAT).replace(tzinfo=UTC)


def _write_durable(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


@dataclass(frozen=True)
class Version:
    path: str
    ts: str
    sha256: str | None
    event: Event
    source: Source
    audit_id: str | None = None
    kind: str | None = None
    from_path: str | None = None
    unconfirmed: bool = False

    @property
    def time(self) -> datetime:
        return parse_ts(self.ts)


class HistoryStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "history"
        self.objects = self.root / ".objects"

    # -- blobs ---------------------------------------------------------------------------

    def _blob_path(self, sha: str) -> Path:
        return self.objects / sha[:2] / sha

    def put_blob(self, data: bytes) -> str:
        sha = version_of(data)
        dst = self._blob_path(sha)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            _write_durable(dst, gzip.compress(data))
            fsync_dir(dst.parent)
        return sha

    def content(self, version: Version) -> bytes | None:
        if version.sha256 is None:
            return None
        return gzip.decompress(self._blob_path(version.sha256).read_bytes())

    # -- pointers ------------------------------------------------------------------------

    def _dir(self, path: str) -> Path:
        return self.root / path

    @staticmethod
    def _load(path: str, file: Path) -> Version:
        raw: dict[str, Any] = json.loads(file.read_text())
        return Version(path=path, ts=file.stem, **raw)

    def versions(self, path: str) -> list[Version]:
        d = self._dir(path)
        if not d.is_dir():
            return []
        return [self._load(path, f) for f in sorted(d.glob("*.json"))]

    def head(self, path: str) -> Version | None:
        versions = self.versions(path)
        return versions[-1] if versions else None

    def record(
        self,
        path: str,
        data: bytes | None,
        *,
        event: Event,
        source: Source,
        ts: datetime | str | None = None,
        audit_id: str | None = None,
        kind: str | None = None,
        from_path: str | None = None,
        unconfirmed: bool = False,
    ) -> Version:
        """Append one version. Idempotent on (audit_id, kind) when an audit id is given."""
        if audit_id is not None:
            for existing in self.versions(path):
                if existing.audit_id == audit_id and existing.kind == kind:
                    return existing
        stamp = ts if isinstance(ts, str) else format_ts(ts or datetime.now(UTC))
        fields: dict[str, Any] = {
            "sha256": self.put_blob(data) if data is not None else None,
            "event": event,
            "source": source,
        }
        for key, value in (("audit_id", audit_id), ("kind", kind), ("from_path", from_path)):
            if value is not None:
                fields[key] = value
        if unconfirmed:
            fields["unconfirmed"] = True
        d = self._dir(path)
        d.mkdir(parents=True, exist_ok=True)
        name, n = stamp, 0
        while (d / f"{name}.json").exists():
            n += 1
            name = f"{stamp}_{n:04d}"  # "_" sorts after ".json", keeping insertion order
        _write_durable(d / f"{name}.json", json.dumps(fields).encode())
        fsync_dir(d)
        return Version(path=path, ts=name, **fields)

    def record_entry(
        self, entry: SpoolEntry, *, unconfirmed: bool = False, include_post: bool = True
    ) -> None:
        """Record a spool transaction: the pre-image (an unrecorded web edit) then the MCP write."""
        if entry.has_pre:
            self.record(
                entry.path, entry.pre_bytes(), event="modify", source="web",
                ts=entry.ts_pre, audit_id=entry.audit_id, kind="pre",
            )  # fmt: skip
        if include_post:
            target = entry.new_path or entry.path
            self.record(
                target, entry.post_bytes(), event=entry.event, source="mcp", ts=entry.ts_post,
                audit_id=entry.audit_id, kind="post",
                from_path=entry.path if entry.new_path else None, unconfirmed=unconfirmed,
            )  # fmt: skip

    def content_at(self, path: str, at: datetime) -> bytes | None:
        """Content as it was at `at`: the newest version at or before it (None if deleted/absent)."""
        current: Version | None = None
        for v in self.versions(path):
            if v.time <= at:
                current = v
        return self.content(current) if current else None

    def _all_paths(self) -> list[str]:
        paths = {
            str(f.parent.relative_to(self.root))
            for f in self.root.rglob("*.json")
            if not f.relative_to(self.root).parts[0].startswith(".")
        }
        return sorted(paths)

    def deleted_under(self, prefix: str) -> dict[str, bytes]:
        """Files under `prefix` whose newest version is a tombstone, with their last content."""
        out: dict[str, bytes] = {}
        for path in self._all_paths():
            if not path.startswith(prefix):
                continue
            versions = self.versions(path)
            if versions and versions[-1].sha256 is None:
                last = next((v for v in reversed(versions) if v.sha256 is not None), None)
                if last is not None:
                    content = self.content(last)
                    if content is not None:
                        out[path] = content
        return out

    def gc(self, *, now: datetime, retention_days: int, min_versions: int) -> int:
        """Drop versions older than the retention window, always keeping the newest
        `min_versions` per file; then delete blobs no remaining version references.
        The spool is never touched. Returns the number of versions removed."""
        cutoff = now - timedelta(days=retention_days)
        removed = 0
        referenced: set[str] = set()
        by_dir: dict[Path, list[Path]] = defaultdict(list)
        for f in self.root.rglob("*.json"):
            if not f.relative_to(self.root).parts[0].startswith("."):
                by_dir[f.parent].append(f)
        for files in by_dir.values():
            files.sort()
            keep_from = max(0, len(files) - min_versions)
            for i, f in enumerate(files):
                if i < keep_from and parse_ts(f.stem) < cutoff:
                    f.unlink()
                    removed += 1
                    continue
                sha = json.loads(f.read_text()).get("sha256")
                if sha:
                    referenced.add(sha)
        if self.objects.is_dir():
            for blob in self.objects.rglob("*"):
                if blob.is_file() and blob.name not in referenced:
                    blob.unlink()
        return removed


# --------------------------------------------------------------------------------------------
# Spool (D21)


@dataclass(frozen=True)
class SpoolEntry:
    dir: Path
    state: Literal["prepared", "committed"]
    audit_id: str
    path: str
    new_path: str | None
    post_sha256: str
    pre_sha256: str | None
    pre_known: bool  # pre.json exists (it can be missing only after a power loss)
    event: Event
    has_pre: bool
    ts_pre: str
    ts_post: str
    tool: str
    credential_id: str | None
    group_id: str | None

    def post_bytes(self) -> bytes:
        return (self.dir / "post.blob").read_bytes()

    def pre_bytes(self) -> bytes:
        return (self.dir / "pre.blob").read_bytes()


class Spool:
    def __init__(self, data_dir: Path) -> None:
        self.inbox = data_dir / "history" / ".inbox"

    def prepare(
        self,
        audit_id: str,
        path: str,
        post: bytes,
        *,
        tool: str,
        credential_id: str | None,
        new_path: str | None = None,
        group_id: str | None = None,
    ) -> Path:
        """Step 2a: make everything that doesn't depend on the current file durable."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        d = self.inbox / f"{audit_id}.prepared"
        d.mkdir()
        _write_durable(d / "post.blob", post)
        now = datetime.now(UTC)
        txn = {
            "audit_id": audit_id,
            "path": path,
            "new_path": new_path,
            "post_sha256": version_of(post),
            "tool": tool,
            "credential_id": credential_id,
            "group_id": group_id,
            "ts_pre": format_ts(now),
            "ts_post": format_ts(now + timedelta(microseconds=1)),
        }
        _write_durable(d / "txn.json", json.dumps(txn).encode())
        fsync_dir(d)
        fsync_dir(self.inbox)
        return d

    def write_pre(self, txn_dir: Path, current: bytes | None, *, record_pre: bool) -> None:
        """Step 2b: the current file's hash (always) and bytes (when not yet in history)."""
        txn = json.loads((txn_dir / "txn.json").read_text())
        if current is not None and record_pre:
            _write_durable(txn_dir / "pre.blob", current)
        else:
            (txn_dir / "pre.blob").unlink(missing_ok=True)
        event: Event = (
            "rename" if txn.get("new_path") else ("modify" if current is not None else "create")
        )
        pre = {
            "pre_sha256": version_of(current) if current is not None else None,
            "event": event,
            "has_pre": current is not None and record_pre,
        }
        _write_durable(txn_dir / "pre.json", json.dumps(pre).encode())
        fsync_dir(txn_dir)

    def commit(self, txn_dir: Path) -> Path:
        """Step 5: the rename is the commit point."""
        committed = txn_dir.with_suffix(".committed")
        txn_dir.rename(committed)
        fsync_dir(self.inbox)
        return committed

    def drop(self, txn_dir: Path) -> None:
        shutil.rmtree(txn_dir, ignore_errors=True)
        if self.inbox.exists():
            fsync_dir(self.inbox)

    def entries(self) -> list[SpoolEntry]:
        """Complete entries (txn.json present), oldest first; pre.json merged in."""
        if not self.inbox.is_dir():
            return []
        out: list[SpoolEntry] = []
        for d in sorted(self.inbox.iterdir()):
            if d.suffix not in (".prepared", ".committed") or not (d / "txn.json").is_file():
                continue
            txn = json.loads((d / "txn.json").read_text())
            pre_file = d / "pre.json"
            pre = json.loads(pre_file.read_text()) if pre_file.is_file() else {}
            out.append(
                SpoolEntry(
                    dir=d,
                    state="committed" if d.suffix == ".committed" else "prepared",
                    audit_id=txn["audit_id"],
                    path=txn["path"],
                    new_path=txn.get("new_path"),
                    post_sha256=txn["post_sha256"],
                    pre_sha256=pre.get("pre_sha256"),
                    pre_known=pre_file.is_file(),
                    event=pre.get("event", "modify"),
                    has_pre=bool(pre.get("has_pre")) and (d / "pre.blob").is_file(),
                    ts_pre=txn["ts_pre"],
                    ts_post=txn["ts_post"],
                    tool=txn["tool"],
                    credential_id=txn.get("credential_id"),
                    group_id=txn.get("group_id"),
                )
            )
        return out

    def ack(self, entry: SpoolEntry) -> None:
        """The watcher acknowledges by deleting the entry after its pointers are durable."""
        self.drop(entry.dir)


def classify(
    current_sha256: str | None, *, pre_sha256: str | None, post_sha256: str, pre_known: bool
) -> Outcome:
    """D21 recovery for a `.prepared` entry left by a dead vault-mcp, from the file's hash now."""
    if current_sha256 == post_sha256:
        return "applied"
    if pre_known and current_sha256 == pre_sha256:
        return "aborted"
    return "unconfirmed"
