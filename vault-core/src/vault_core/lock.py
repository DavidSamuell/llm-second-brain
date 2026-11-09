"""The advisory vault lock shared by vault-mcp, vault-watcher, git-sync and the scripts (D6).

`flock(2)` on `<data>/locks/vault.lock`, so it interoperates with `flock(1)` in bash and is
released automatically if the holder dies. The holder writes its name to `vault.owner`.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


def lock_path(data_dir: Path) -> Path:
    return data_dir / "locks" / "vault.lock"


def owner_path(data_dir: Path) -> Path:
    return data_dir / "locks" / "vault.owner"


def read_owner(data_dir: Path) -> str | None:
    """Name of the last holder; only meaningful while the caller holds the lock."""
    try:
        return owner_path(data_dir).read_text().strip() or None
    except FileNotFoundError:
        return None


def _acquire(data_dir: Path, owner: str) -> int:
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        owner_path(data_dir).write_text(owner)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def vault_lock(data_dir: Path, owner: str) -> Iterator[None]:
    """Blocking exclusive hold of the vault lock."""
    fd = _acquire(data_dir, owner)
    try:
        yield
    finally:
        _release(fd)


class AsyncVaultLock:
    """Async hold for one process: an asyncio.Lock serialises tasks, and the blocking flock
    runs in a worker thread so the event loop keeps serving while another process holds it."""

    def __init__(self, data_dir: Path, owner: str) -> None:
        self._data_dir = data_dir
        self._owner = owner
        self._local = asyncio.Lock()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        async with self._local:
            fd = await asyncio.to_thread(_acquire, self._data_dir, self._owner)
            try:
                yield
            finally:
                _release(fd)
