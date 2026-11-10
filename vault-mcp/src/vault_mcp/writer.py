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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from vault_core import frontmatter, review_queue
from vault_core.audit import AuditLog, new_id
from vault_core.fsio import fsync_dir, replace_from_temp, version_of, write_temp_sibling
from vault_core.history import HistoryStore, Spool
from vault_core.lock import AsyncVaultLock
from vault_core.paths import PathError, is_hidden, normalize, resolve_in_vault, through_symlink
from vault_core.policy import Decision, Policy, check_read, check_write
from vault_core.secrets import scan_added_lines
from vault_mcp import sections
from vault_mcp.errors import VaultToolError
from vault_mcp.links import VaultFiles, links_to, rebase_relative_links, rewrite_links
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

    async def review_queue_add(
        self, cred: Credential, page: str, issue: str, suggestion: str
    ) -> dict[str, str]:
        """Propose a change as a task in `_system/review-queue.md` (D15). Any write credential
        may add to the queue, whatever its folders: it is how agents ask about pages they
        can't edit. The page itself must be readable by the credential."""
        try:
            page_rel = normalize(page)
        except PathError as exc:
            raise VaultToolError(exc.code, str(exc), page=page) from None
        if not check_read(page_rel, self.policy, cred.folders).allowed:
            raise VaultToolError(
                "forbidden", "you can only propose changes to pages you can read", page=page_rel
            )
        item_id = new_id()
        item = review_queue.format_item(
            page_rel,
            issue,
            suggestion,
            by=cred.name,
            owner=cred.owner,
            ts=datetime.now(UTC),
            item_id=item_id,
        )

        def build(current: bytes | None) -> bytes:
            text = current.decode("utf-8", "replace") if current is not None else None
            return review_queue.add_item(text, item).encode()

        result = await self.apply(
            cred, "review_queue_add", review_queue.QUEUE_PATH, build,
            base_version=ANY, retries=APPEND_RETRIES, queue=True,
        )  # fmt: skip
        return {"path": result.path, "version": result.version, "item_id": f"rq-{item_id.lower()}"}

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
        queue: bool = False,
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
                outcome = await _finish_even_if_cancelled(
                    asyncio.to_thread(
                        self._attempt, cred, tool, rel, target, build, base_version, queue
                    )
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
        queue: bool = False,
    ) -> tuple[Path, WriteResult] | None:
        """One locked attempt. Returns None when the file changed under us and the tool may retry."""
        current = target.read_bytes() if target.is_file() else None
        maintainer = (
            frontmatter.maintainer_of(current.decode("utf-8", "replace"))
            if current is not None
            else None
        )
        decision = (
            Decision(True)  # the review queue is open to every write credential (D15)
            if queue
            else check_write(
                rel, self.policy, cred.folders, exists=current is not None, maintainer=maintainer
            )
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

    # -- move (D12) ----------------------------------------------------------------------------

    def _vault_files(self) -> tuple[VaultFiles, list[str]]:
        """(link targets, pages to scan for links). Targets are every non-hidden, non-symlinked
        file (Obsidian doesn't resolve links to dotfiles). The scan also includes hidden
        Markdown pages, except Obsidian's trash and git internals, so a link in a page agents
        can't see still blocks a move (D12) instead of being left stale."""
        targets, pages = [], []
        for p in self.vault_dir.rglob("*"):
            rel = p.relative_to(self.vault_dir).as_posix()
            if not p.is_file() or through_symlink(self.vault_dir, rel):
                continue
            if not is_hidden(rel):
                targets.append(rel)
            if rel.endswith(".md") and rel.split("/", 1)[0] not in (".trash", ".git"):
                pages.append(rel)
        return VaultFiles(targets), sorted(pages)

    async def move(
        self, cred: Credential, path: str, new_path: str, base_version: str
    ) -> dict[str, Any]:
        """Rename a page and update every link to it, or change nothing."""
        if not cred.allows("write"):
            raise VaultToolError("forbidden", "this credential is read-only", path=path)
        rel, source = self._target(path)
        new_rel, dest = self._target(new_path)
        if rel == new_rel:
            raise VaultToolError("invalid", "path and new_path are the same", path=rel)
        if not self._heartbeat_fresh():
            raise VaultToolError(
                "history_pending", "vault-watcher is not running, so the move would have no history; nothing was changed",
                path=rel, applied=False,
            )  # fmt: skip
        async with self.lock.hold():
            committed, moved_version, updated = await _finish_even_if_cancelled(
                asyncio.to_thread(
                    self._attempt_move, cred, rel, source, new_rel, dest, base_version
                )
            )
        for txn, result in committed:
            await self._await_ack(txn, result)
        return {
            "path": new_rel,
            "from_path": rel,
            "version": moved_version,
            "updated_links_in": updated,
        }

    def _check_writable(self, cred: Credential, rel: str, current: bytes | None) -> None:
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

    def _attempt_move(
        self, cred: Credential, rel: str, source: Path, new_rel: str, dest: Path, base_version: str
    ) -> tuple[list[tuple[Path, WriteResult]], str, list[str]]:
        if not source.is_file():
            raise VaultToolError("not_found", "no such file", path=rel)
        current = source.read_bytes()
        self._check_writable(cred, rel, current)
        self._check_writable(cred, new_rel, None)
        if os.path.lexists(dest):
            raise VaultToolError("conflict", "new_path already exists", path=rel, new_path=new_rel)
        if base_version != version_of(current):
            raise VaultToolError(
                "conflict", "the file changed since you read it; read it again and retry",
                path=rel, current_version=version_of(current),
            )  # fmt: skip

        # Preflight: every page linking to the file must be writable, or nothing happens.
        files, pages = self._vault_files()
        linkers: dict[str, bytes] = {}
        visible_blockers: list[str] = []
        hidden_blockers = 0
        for other in pages:
            if other == rel:
                continue
            data = (self.vault_dir / other).read_bytes()
            if not links_to(data.decode("utf-8", "replace"), other, rel, files):
                continue
            maintainer = frontmatter.maintainer_of(data.decode("utf-8", "replace"))
            if check_write(
                other, self.policy, cred.folders, exists=True, maintainer=maintainer
            ).allowed:
                linkers[other] = data
            elif check_read(other, self.policy, cred.folders).allowed:
                visible_blockers.append(other)
            else:
                hidden_blockers += 1
        if visible_blockers or hidden_blockers:
            raise VaultToolError(
                "forbidden",
                "pages you can't edit link to this file, so it can't be moved without breaking their "
                "links; propose the move with review_queue_add",
                path=rel, blocking_files=visible_blockers, hidden_blockers=hidden_blockers,
            )  # fmt: skip

        # Build every new content up front.
        text = current.decode("utf-8", "replace")
        moved_text = rewrite_links(
            rebase_relative_links(text, rel, new_rel), new_rel, rel, new_rel, files
        )
        plan: list[tuple[str, Path, bytes, bytes, str | None]] = [
            (
                o,
                self.vault_dir / o,
                data,
                rewrite_links(data.decode("utf-8", "replace"), o, rel, new_rel, files).encode(),
                None,
            )
            for o, data in linkers.items()
        ]
        plan.append((rel, dest, current, moved_text.encode(), new_rel))

        # D21 for a group: prepare all, re-check all, apply all, audit all, commit all.
        group_id = new_id()
        staged: list[_Staged] = []
        try:
            for o, target, before, after, moved_to in plan:
                audit_id = new_id()
                txn = self.spool.prepare(
                    audit_id,
                    o,
                    after,
                    tool="move",
                    credential_id=cred.id,
                    new_path=moved_to,
                    group_id=group_id,
                )
                staged.append(
                    _Staged(
                        o, self.vault_dir / o, target, txn, None, before, after, moved_to, audit_id
                    )
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                staged[-1].tmp = write_temp_sibling(target, after, tag=secrets.token_hex(4))
                head = self.history.head(o)
                self.spool.write_pre(
                    txn, before, record_pre=head is None or head.sha256 != version_of(before)
                )
            for s in staged:  # last-moment re-check of every file in the group
                if not s.current_path.is_file() or s.current_path.read_bytes() != s.before:
                    raise _Changed
        except _Changed:
            self._discard_group(staged)
            raise VaultToolError(
                "conflict", "a file changed while moving; nothing was changed; retry", path=rel
            ) from None
        except BaseException:
            self._discard_group(staged)
            raise
        results: list[tuple[Path, WriteResult]] = []
        for k, s in enumerate(staged):
            assert s.tmp is not None  # noqa: S101 - set for every staged entry above
            # D21 step 3 per file: a save to this file during the earlier replacements must
            # not be overwritten. Stop here; the entries stay prepared, so vault-watcher
            # classifies each (applied / aborted / unconfirmed) and alerts on the partial group.
            unchanged = s.current_path.is_file() and s.current_path.read_bytes() == s.before
            if not unchanged or (s.moved_to and os.path.lexists(s.target)):
                for rest in staged[k:]:
                    if rest.tmp is not None:
                        with contextlib.suppress(FileNotFoundError):
                            rest.tmp.unlink()
                raise VaultToolError(
                    "conflict",
                    "a file changed while the move was being applied; the move is only partly "
                    "applied and an alert was raised for a human to finish or undo it",
                    path=rel, group_id=group_id, applied=[r.rel for r in staged[:k]],
                    not_applied=[r.rel for r in staged[k:]],
                )  # fmt: skip
            replace_from_temp(s.tmp, s.target)
            if s.moved_to:
                s.current_path.unlink()
                fsync_dir(s.current_path.parent)
            self.audit.append(
                id=s.audit_id, durable=True, service="vault-mcp", tool="move", result="ok",
                credential_id=cred.id, credential_name=cred.name, owner=cred.owner, path=s.rel,
                new_path=s.moved_to, old_version=version_of(s.before), new_version=version_of(s.after),
                detail=None if s.moved_to else f"link update for move of {rel}", group_id=group_id,
            )  # fmt: skip
            results.append(
                (s.txn, WriteResult(s.moved_to or s.rel, version_of(s.after), s.audit_id))
            )
        committed = [(self.spool.commit(txn), result) for txn, result in results]
        return committed, version_of(plan[-1][3]), sorted(linkers)

    def _discard_group(self, staged: list[_Staged]) -> None:
        for s in staged:
            self.spool.drop(s.txn)
            if s.tmp is not None:
                with contextlib.suppress(FileNotFoundError):
                    s.tmp.unlink()


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


async def _finish_even_if_cancelled[T](work: Awaitable[T]) -> T:
    """A cancelled tool call must not release the vault lock while its worker thread is still
    writing: wait for the thread (ignoring further cancellations), then re-raise."""
    future = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                raise
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return result


@dataclass
class _Staged:
    rel: str
    current_path: Path
    target: Path
    txn: Path
    tmp: Path | None
    before: bytes
    after: bytes
    moved_to: str | None
    audit_id: str


class _Changed(Exception):
    """The file changed between the permission check and the rename."""


def write_result(result: WriteResult) -> dict[str, Any]:
    return {"path": result.path, "version": result.version}
