"""`scripts/backup` and `scripts/restore` (SPEC §12, §16.16, plan D37).

Backup runs while the stack is up: it holds the vault lock, so no MCP write, git-sync commit or
watcher snapshot happens in between; SQLite databases are copied with the online backup API.
The archive holds `vault/`, `data/` (history, tokens, leases, audit, Ignis state…), optionally
`config/` (`.env`, `vault-mcp.yaml`), and `MANIFEST.json` with a sha256 for every file.

Restore needs the stack stopped and empty target folders (or `force`), verifies every hash
before writing anything, and extracts with tarfile's `data` filter (no links out, no absolute
paths, no device files).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vault_core.lock import vault_lock

SKIP_DATA = ("locks/",)  # lock files are recreated; never restore a held lock
SKIP_SUFFIXES = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    """Backup or restore refused; the message says why."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def _snapshot_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def create_backup(
    vault_dir: Path, data_dir: Path, out: Path, config: list[Path] | None = None
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": 1,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {},
    }
    agents = vault_dir / "AGENTS.md"
    if agents.is_file():
        from vault_watcher.upgrade import vault_schema_version

        manifest["vault_schema_version"] = vault_schema_version(vault_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(partial, "w:gz") as tar:
        staging = Path(tmp)

        def add(path: Path, name: str) -> None:
            manifest["files"][name] = _sha(path)
            tar.add(path, arcname=name, recursive=False)

        def add_dirs(root: Path, prefix: str) -> None:
            """Folders too, so empty ones (Ignis creates some) come back on restore."""
            for d in sorted(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()):
                rel = d.relative_to(root).as_posix()
                if prefix == "data" and (rel + "/").startswith(SKIP_DATA):
                    continue
                tar.add(d, arcname=f"{prefix}/{rel}", recursive=False)

        with vault_lock(data_dir, "backup"):
            add_dirs(vault_dir, "vault")
            add_dirs(data_dir, "data")
            for path in _files(vault_dir):
                add(path, f"vault/{path.relative_to(vault_dir).as_posix()}")
            for path in _files(data_dir):
                rel = path.relative_to(data_dir).as_posix()
                if rel.startswith(SKIP_DATA) or rel.endswith(SKIP_SUFFIXES):
                    continue
                if _is_sqlite(path):
                    copy = staging / rel
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    _snapshot_db(path, copy)
                    copy.chmod(path.stat().st_mode & 0o777)
                    add(copy, f"data/{rel}")
                else:
                    add(path, f"data/{rel}")
        for path in config or []:
            if path.is_file():
                add(path, f"config/{path.name}")
        body = json.dumps(manifest, indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mtime, info.mode = len(body), int(datetime.now(UTC).timestamp()), 0o600
        tar.addfile(info, io.BytesIO(body))
    partial.chmod(0o600)  # it contains tokens.db and possibly .env
    partial.replace(out)
    return manifest


def restore(
    archive: Path, vault_dir: Path, data_dir: Path, config_out: Path, *, force: bool = False
) -> dict[str, Any]:
    for target in (vault_dir, data_dir):
        if any(target.iterdir()) and not force:
            raise BackupError(
                f"{target} is not empty; empty it first (or pass --force to replace its contents)"
            )
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        manifest_path = staging / "MANIFEST.json"
        if not manifest_path.is_file():
            raise BackupError("not a kit backup: MANIFEST.json is missing")
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        listed = manifest.get("files", {})
        present = {p.relative_to(staging).as_posix() for p in _files(staging)} - {"MANIFEST.json"}
        if present != set(listed):
            raise BackupError(
                f"archive contents don't match MANIFEST.json ({len(present ^ set(listed))} files differ)"
            )
        bad = [name for name, digest in listed.items() if _sha(staging / name) != digest]
        if bad:
            raise BackupError(
                f"checksum mismatch in {len(bad)} file(s), e.g. {bad[0]}; nothing was restored"
            )
        for target in (vault_dir, data_dir):
            for child in target.iterdir():
                shutil.rmtree(
                    child
                ) if child.is_dir() and not child.is_symlink() else child.unlink()
        for src_name, target in (("vault", vault_dir), ("data", data_dir)):
            src = staging / src_name
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True, symlinks=False)
        if (staging / "config").is_dir():
            config_out.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging / "config", config_out, dirs_exist_ok=True)
    return manifest
