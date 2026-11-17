"""Vault-relative path handling (plan D8).

Tool paths are vault-relative POSIX strings. Anything non-canonical is rejected rather
than silently normalised, so a path always means exactly one file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

PathErrorCode = Literal["invalid", "forbidden"]


class PathError(ValueError):
    def __init__(self, code: PathErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: PathErrorCode = code


def normalize(raw: str) -> str:
    """Return `raw` if it is a canonical vault-relative file path, else raise PathError."""
    if not raw:
        raise PathError("invalid", "path is empty")
    if "\x00" in raw or "\\" in raw:
        raise PathError("invalid", f"path contains a NUL or backslash: {raw!r}")
    if raw.startswith("/"):
        raise PathError("invalid", f"path must be relative to the vault: {raw!r}")
    for segment in raw.split("/"):
        if segment in ("", ".", ".."):
            raise PathError("invalid", f"path is not canonical: {raw!r}")
    return raw


def is_hidden(path: str) -> bool:
    """Dot-segments (.obsidian, .git, .trash, dotfiles, temp files) are never served."""
    return any(segment.startswith(".") for segment in path.split("/"))


def through_symlink(vault_root: Path, rel: str) -> bool:
    """Whether `rel` (or any folder on its way) is a symlink. The vault root itself may be one."""
    root = vault_root.resolve()
    return (root / rel).resolve() != root / rel


def resolve_in_vault(vault_root: Path, raw: str) -> Path:
    """Resolve a tool path to an absolute path inside the vault.

    Symlinks are refused even when they point inside the vault: policy (folder scope,
    read.deny, write.allow) is decided on the path string, so `wiki/x.md -> ../private/y.md`
    would otherwise inherit wiki/'s permissions."""
    path = normalize(raw)
    if is_hidden(path):
        raise PathError("forbidden", f"hidden path: {path}")
    root = vault_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PathError("forbidden", f"path escapes the vault: {path}")
    if target != root / path:
        raise PathError("forbidden", f"path goes through a symlink: {path}")
    return target
