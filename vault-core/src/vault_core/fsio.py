"""File versions and atomic same-directory replacement (D7).

A version is the lowercase hex sha256 of a file's raw bytes; a missing file has version None.
Writes go to a hidden temp sibling (`.<name>.vault-mcp-<tag>.tmp`), are fsynced, then renamed
over the target, so readers (Ignis, the watcher) never see a half-written file. The two
primitives are separate because the D21 handoff makes the temp file durable before it reads
the current file, then renames later.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
from pathlib import Path

_TEMP_RE = re.compile(r"^\..+\.vault-mcp-[0-9a-f]+\.tmp$")
DEFAULT_MODE = 0o644


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_version(path: Path) -> str | None:
    try:
        return version_of(path.read_bytes())
    except FileNotFoundError:
        return None


def temp_sibling(target: Path, tag: str) -> Path:
    return target.with_name(f".{target.name}.vault-mcp-{tag}.tmp")


def is_temp_name(name: str) -> bool:
    return _TEMP_RE.match(name) is not None


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_temp_sibling(target: Path, data: bytes, tag: str | None = None) -> Path:
    """Write `data` durably to a temp file next to `target`, with the target's current mode."""
    tmp = temp_sibling(target, tag or secrets.token_hex(4))
    try:
        mode = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = DEFAULT_MODE
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        try:
            os.fchmod(fd, mode)  # O_CREAT mode is filtered by umask
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return tmp


def replace_from_temp(tmp: Path, target: Path) -> None:
    tmp.replace(target)
    fsync_dir(target.parent)


def atomic_write(target: Path, data: bytes) -> None:
    tmp = write_temp_sibling(target, data)
    try:
        replace_from_temp(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
