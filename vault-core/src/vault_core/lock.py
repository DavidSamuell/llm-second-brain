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


def _open_lock(data_dir: Path) -> int:
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire(data_dir: Path, owner: str) -> int:
    fd = _open_lock(data_dir)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    try:
        owner_path(data_dir).write_text(owner)
    except BaseException:
        _release(fd)
        raise
    return fd


def _try_acquire(data_dir: Path, owner: str) -> int | None:
    fd = _open_lock(data_dir)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    try:
        owner_path(data_dir).write_text(owner)
    except BaseException:
        _release(fd)
        raise
    return fd


@contextmanager
def vault_lock(data_dir: Path, owner: str) -> Iterator[None]:
    """Blocking exclusive hold of the vault lock."""
    fd = _acquire(data_dir, owner)
    try:
        yield
    finally:
        _release(fd)


class AsyncVaultLock:
    """Async hold for one process. An asyncio.Lock serialises tasks; the flock is taken with
    non-blocking attempts and short sleeps, so the event loop keeps serving while another
    process holds it, and a cancelled waiter never leaves a stray acquisition behind."""

    POLL_MIN_S = 0.002
    POLL_MAX_S = 0.05

    def __init__(self, data_dir: Path, owner: str) -> None:
        self._data_dir = data_dir
        self._owner = owner
        self._local = asyncio.Lock()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        async with self._local:
            delay = self.POLL_MIN_S
            while (fd := _try_acquire(self._data_dir, self._owner)) is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.POLL_MAX_S)
            try:
                yield
            finally:
                _release(fd)
