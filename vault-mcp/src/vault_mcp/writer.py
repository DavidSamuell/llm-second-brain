"""The single write path for every MCP tool that changes a file (plan D7, D9, D21, D24).

Under the vault lock: permission → base_version → build → size → secrets → spool prepare →
durable pre-image → final re-read and compare → rename → durable audit line → commit.
Then, outside the lock, wait for vault-watcher's acknowledgement (§16.15).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from vault_core import frontmatter
from vault_core.audit import AuditLog, new_id
from vault_core.fsio import fsync_dir, replace_from_temp, version_of, write_temp_sibling
from vault_core.history import HistoryStore, Spool
from vault_core.lock import AsyncVaultLock
from vault_core.paths import PathError, normalize, resolve_in_vault
from vault_core.policy import Policy, check_write
from vault_core.secrets import scan_added_lines
from vault_mcp import sections
from vault_mcp.errors import VaultToolError
from vault_mcp.store import Credential

ANY: Final = object()  # "no base_version check" (append-style tools)
HEARTBEAT_STALE_S = 30.0
APPEND_RETRIES = 3

_REFUSALS = {
    "hidden": "hidden paths are never written",
    "denied": "path is in read.deny",
    "out_of_scope": "path is outside this credential's folders",
    "human_page": "this page has maintainer: human; propose the change with review_queue_add instead",
    "not_in_write_allow": "agents may only write under write.allow or to agent/shared pages; "
    "use review_queue_add to propose the change",
}


@dataclass(frozen=True)
class WriteResult:
    path: str
    version: str
    audit_id: str


class VaultWriter:
    def __init__(
        self,
        vault_dir: Path,
        data_dir: Path,
        policy: Policy,
        *,
        ack_timeout_s: float = 5.0,
        lock: AsyncVaultLock | None = None,
    ) -> None:
        self.vault_dir = vault_dir
        self.data_dir = data_dir
        self.policy = policy
        self.ack_timeout_s = ack_timeout_s
        self.audit = AuditLog(data_dir / "audit.log")
        self.spool = Spool(data_dir)
        self.history = HistoryStore(data_dir)
        self.heartbeat = data_dir / "vault-watcher" / "heartbeat"
        self.lock = lock or AsyncVaultLock(data_dir, "vault-mcp")

    # -- public tools ----------------------------------------------------------------------

    async def write(
        self, cred: Credential, path: str, content: str, base_version: str | None
    ) -> dict[str, str]:
        """Create (base_version None) or replace (base_version = the version you read)."""
        result = await self.apply(
            cred, "write", path, lambda _: content.encode(), base_version=base_version
        )
        return {"path": result.path, "version": result.version}

    async def append(self, cred: Credential, path: str, text: str) -> dict[str, str]:
        """Append to an existing file, on a new line."""

        def build(current: bytes | None) -> bytes:
            if current is None:
                raise VaultToolError("not_found", "no such file; create it with write", path=path)
            boundary = b"" if not current or current.endswith(b"\n") else b"\n"
            return current + boundary + text.encode()

        result = await self.apply(
            cred, "append", path, build, base_version=ANY, retries=APPEND_RETRIES
        )
        return {"path": result.path, "version": result.version}

    async def replace_section(
        self, cred: Credential, path: str, heading: str, content: str, base_version: str
    ) -> dict[str, str]:
        """Replace one section's body (D11), checked against the version you read."""
        build = _section_edit(path, lambda text: sections.replace_section(text, heading, content))
        result = await self.apply(cred, "replace_section", path, build, base_version=base_version)
        return {"path": result.path, "version": result.version}

    async def insert_after(
        self, cred: Credential, path: str, heading: str, content: str
    ) -> dict[str, str]:
        """Insert directly below a heading (D11); like append, no base_version needed."""
        build = _section_edit(path, lambda text: sections.insert_after(text, heading, content))
        result = await self.apply(
            cred, "insert_after", path, build, base_version=ANY, retries=APPEND_RETRIES
        )
        return {"path": result.path, "version": result.version}

    # -- the pipeline ----------------------------------------------------------------------

    def _target(self, path: str) -> tuple[str, Path]:
        try:
            rel = normalize(path)
            return rel, resolve_in_vault(self.vault_dir, rel)
        except PathError as exc:
            raise VaultToolError(exc.code, str(exc), path=path) from None

    def _heartbeat_fresh(self) -> bool:
        try:
            return time.time() - self.heartbeat.stat().st_mtime < HEARTBEAT_STALE_S
        except FileNotFoundError:
            return False

    async def apply(
        self,
        cred: Credential,
        tool: str,
        path: str,
        build: Callable[[bytes | None], bytes],
        *,
        base_version: object = ANY,
        retries: int = 1,
    ) -> WriteResult:
        if not cred.allows("write"):
            raise VaultToolError("forbidden", "this credential is read-only", path=path)
        rel, target = self._target(path)
        if not self._heartbeat_fresh():
            raise VaultToolError(
                "history_pending", "vault-watcher is not running, so the write would have no history; nothing was changed",
                path=rel, applied=False,
            )  # fmt: skip
        async with self.lock.hold():
            for attempt in range(retries):
                # Blocking file I/O and fsyncs run off the event loop; the async lock serialises writers.
                outcome = await asyncio.to_thread(
                    self._attempt, cred, tool, rel, target, build, base_version
                )
                if outcome is not None:
                    break
                if attempt == retries - 1:
                    raise VaultToolError(
                        "conflict",
                        "the file kept changing while writing; nothing was changed",
                        path=rel,
                    )
        assert outcome is not None  # noqa: S101 - loop above either breaks with a value or raises
        committed, result = outcome
        await self._await_ack(committed, result)
        return result

    def _attempt(
        self,
        cred: Credential,
        tool: str,
        rel: str,
        target: Path,
        build: Callable[[bytes | None], bytes],
        base_version: object,
    ) -> tuple[Path, WriteResult] | None:
        """One locked attempt. Returns None when the file changed under us and the tool may retry."""
        current = target.read_bytes() if target.is_file() else None
        maintainer = (
            frontmatter.maintainer_of(current.decode("utf-8", "replace"))
            if current is not None
            else None
        )
        decision = check_write(
            rel, self.policy, cred.folders, exists=current is not None, maintainer=maintainer
        )
        if not decision.allowed:
            reason = decision.reason or "denied"
            raise VaultToolError("forbidden", _REFUSALS[reason], path=rel, reason=reason)
        current_version = version_of(current) if current is not None else None
        if base_version is not ANY and base_version != current_version:
            raise VaultToolError(
                "conflict", "the file changed since you read it; read it again and reapply your edit",
                path=rel, current_version=current_version,
            )  # fmt: skip
        new = build(current)
        limit = int(self.policy.write.max_file_mb * 1024 * 1024)
        if len(new) > limit:
            raise VaultToolError(
                "too_large",
                "the result would exceed max_file_mb",
                path=rel,
                size=len(new),
                limit=limit,
            )
        if self.policy.secrets.reject_on_write:
            old_text = current.decode("utf-8", "replace") if current is not None else None
            findings = scan_added_lines(old_text, new.decode("utf-8", "replace"))
            if findings:
                raise VaultToolError(
                    "secret_detected", "the content contains what looks like a secret; remove it and retry",
                    path=rel, findings=[f.masked() for f in findings],
                )  # fmt: skip

        audit_id = new_id()
        txn = self.spool.prepare(audit_id, rel, new, tool=tool, credential_id=cred.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = write_temp_sibling(target, new, tag=secrets.token_hex(4))
        try:
            # D21 step 2b-3: persist the pre-image, then re-read immediately before renaming.
            now = target.read_bytes() if target.is_file() else None
            if now != current:
                raise _Changed
            head = self.history.head(rel)
            unrecorded = now is not None and (head is None or head.sha256 != version_of(now))
            self.spool.write_pre(txn, now, record_pre=unrecorded)
            again = target.read_bytes() if target.is_file() else None
            if again != now:
                raise _Changed
            replace_from_temp(tmp, target)
        except _Changed:
            self._discard(txn, tmp)
            if base_version is not ANY:
                raise VaultToolError(
                    "conflict", "the file changed while writing; read it again and reapply your edit",
                    path=rel, current_version=version_of(target.read_bytes()) if target.is_file() else None,
                ) from None  # fmt: skip
            return None
        except BaseException:
            self._discard(txn, tmp)
            raise
        new_version = version_of(new)
        self.audit.append(
            id=audit_id, durable=True, service="vault-mcp", tool=tool, result="ok",
            credential_id=cred.id, credential_name=cred.name, owner=cred.owner, path=rel,
            old_version=current_version, new_version=new_version,
        )  # fmt: skip
        committed = self.spool.commit(txn)
        return committed, WriteResult(rel, new_version, audit_id)

    def _discard(self, txn: Path, tmp: Path) -> None:
        self.spool.drop(txn)
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
            fsync_dir(tmp.parent)

    async def _await_ack(self, committed: Path, result: WriteResult) -> None:
        deadline = time.monotonic() + self.ack_timeout_s
        while os.path.lexists(committed):  # noqa: ASYNC240 - a single stat per 5 ms poll
            if time.monotonic() > deadline:
                raise VaultToolError(
                    "history_pending",
                    "the change was applied, but vault-watcher has not recorded its history version yet",
                    path=result.path, applied=True, version=result.version,
                )  # fmt: skip
            await asyncio.sleep(0.005)


def _section_edit(path: str, edit: Callable[[str], str]) -> Callable[[bytes | None], bytes]:
    def build(current: bytes | None) -> bytes:
        if current is None:
            raise VaultToolError("not_found", "no such file", path=path)
        try:
            text = current.decode("utf-8")
        except UnicodeDecodeError:
            raise VaultToolError(
                "invalid", "section edits need a UTF-8 text file", path=path
            ) from None
        return edit(text).encode()

    return build


class _Changed(Exception):
    """The file changed between the permission check and the rename."""


def write_result(result: WriteResult) -> dict[str, Any]:
    return {"path": result.path, "version": result.version}
