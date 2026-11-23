"""Export the canonical vault documents from a kit backup archive, for the GitHub snapshot.

`scripts/publish-vault-snapshot` runs
`python -m vault_watcher.snapshot_export --vault-id UUID ARCHIVE DEST` on the nightly archive
written by `backup.create_backup`. The archive is validated before anything is published:
member paths and types, the manifest and its `vault_id`, every SHA-256 checksum. Only `vault/`
is exported; `data/` and `config/` are checksummed and never written. Editor state, trash and
git metadata are left out; everything else is published or refuses the whole export.

Publication policy: only UTF-8 text without NUL bytes is published, because only text can be
inspected truthfully (by `vault_core.secrets.scan_text`). Images, PDFs, archives, SQLite
databases and any other binary refuse the export by name, as do credential file names,
encrypted text containers (PGP, age, Ansible Vault) and detected secrets. A clean scan is not a
guarantee that a document holds no secret; it is the kit's curated, high-confidence rule set.

Nothing is extracted with `extractall`: each exported file is written by name into a fresh
staging folder beside DEST, which replaces DEST only once the archive has passed every check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from vault_core.secrets import scan_text
from vault_watcher.secret_scan import _as_text

MANIFEST = "MANIFEST.json"
MANIFEST_MAX_BYTES = 64 * 1024 * 1024
FILE_LIMIT_BYTES = 100 * 1024 * 1024  # GitHub rejects files of 100 MiB or more; no LFS
SECTIONS = frozenset({"vault", "data", "config"})
REQUIRED_DOCS = ("AGENTS.md", "Home.md")  # vault-root markers of a real kit vault
CREATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # as create_backup writes it

# Personal editor state, trash and git metadata at the vault root; `.git` at any depth.
EXCLUDED_ROOTS = frozenset({".obsidian", ".trash", ".git"})
# Text that is an opaque, encrypted container: its contents cannot be inspected.
ENCRYPTED_TEXT_MARKERS = (
    "-----BEGIN PGP MESSAGE-----",
    "-----BEGIN AGE ENCRYPTED FILE-----",
    "$ANSIBLE_VAULT;",
)
CREDENTIAL_NAMES = frozenset(
    {
        ".credentials.json",
        "credentials.json",
        ".git-credentials",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        ".dockercfg",
        ".envrc",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ecdsa_sk",
        "id_ed25519",
        "id_ed25519_sk",
    }
)
CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".ppk", ".jks", ".keystore", ".kdbx")
CREDENTIAL_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".docker"})


class SnapshotExportError(Exception):
    """Export refused; the message says why and never contains file contents."""


@dataclass(frozen=True)
class _Manifest:
    created_at: str
    files: dict[str, str]
    vault_id: uuid.UUID


def _check_name(name: str) -> list[str]:
    """Archive member path as components; refuses anything that could leave the target."""
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    ):
        raise SnapshotExportError(f"unsafe member path in archive: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SnapshotExportError(f"unsafe member path in archive: {name!r}")
    return parts


def _check_members(members: list[tarfile.TarInfo]) -> set[str]:
    """Validates paths, types and uniqueness; returns the regular file names."""
    seen: set[str] = set()
    files: set[str] = set()
    for member in members:
        parts = _check_name(member.name)
        if member.name in seen:
            raise SnapshotExportError(f"duplicate member in archive: {member.name}")
        seen.add(member.name)
        if member.isreg() and not member.issparse():
            if member.name != MANIFEST and (parts[0] not in SECTIONS or len(parts) < 2):
                raise SnapshotExportError(f"unexpected member in archive: {member.name}")
            files.add(member.name)
        elif member.isdir():
            if parts[0] not in SECTIONS:
                raise SnapshotExportError(f"unexpected member in archive: {member.name}")
        else:
            raise SnapshotExportError(
                f"archive member is not a regular file or folder: {member.name}"
            )
    for name in seen:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in files:
                raise SnapshotExportError(f"archive member is below a file: {name}")
    if MANIFEST not in files:
        raise SnapshotExportError("not a kit backup: MANIFEST.json is missing")
    return files


def _read_manifest(tar: tarfile.TarFile, member: tarfile.TarInfo) -> _Manifest:
    if member.size > MANIFEST_MAX_BYTES:
        raise SnapshotExportError("MANIFEST.json is too large")
    stream = tar.extractfile(member)
    if stream is None:
        raise SnapshotExportError("MANIFEST.json is not readable")
    try:
        raw: object = json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotExportError("MANIFEST.json is not valid JSON") from None
    if not isinstance(raw, dict):
        raise SnapshotExportError("MANIFEST.json is not an object")
    fmt = raw.get("format")
    if type(fmt) is not int or fmt != 1:
        raise SnapshotExportError("unsupported backup format (expected format 1)")
    created_at = raw.get("created_at")
    if not isinstance(created_at, str):
        raise SnapshotExportError("MANIFEST.json has no created_at")
    try:
        datetime.strptime(created_at, CREATED_AT_FORMAT)
    except ValueError:
        raise SnapshotExportError("MANIFEST.json created_at is not a UTC timestamp") from None
    listed = raw.get("files")
    if not isinstance(listed, dict):
        raise SnapshotExportError("MANIFEST.json has no files table")
    files: dict[str, str] = {}
    for name, digest in listed.items():
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise SnapshotExportError("MANIFEST.json files table has an invalid entry")
        files[name] = digest
    schema = raw.get("vault_schema_version")
    if schema is not None and type(schema) is not int:
        raise SnapshotExportError("MANIFEST.json vault_schema_version is not an integer")
    vault_id = raw.get("vault_id")
    if not isinstance(vault_id, str):
        raise SnapshotExportError("MANIFEST.json has no vault_id; make a fresh backup")
    return _Manifest(created_at, files, _parse_vault_id(vault_id, "MANIFEST.json vault_id"))


def _parse_vault_id(value: str, what: str) -> uuid.UUID:
    """Canonical lowercase hyphenated form only, as `str(uuid.uuid4())` writes it."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise SnapshotExportError(f"{what} is not a UUID") from None
    if str(parsed) != value:
        raise SnapshotExportError(f"{what} is not a UUID in canonical form")
    return parsed


def _is_excluded(parts: list[str]) -> bool:
    return parts[0] in EXCLUDED_ROOTS or ".git" in parts


def _is_credential_path(parts: list[str]) -> bool:
    name = parts[-1].lower()
    return (
        name.startswith(".env.")
        or name.endswith(".env")
        or name in CREDENTIAL_NAMES
        or name.endswith(CREDENTIAL_SUFFIXES)
        or any(part.lower() in CREDENTIAL_DIRS for part in parts[:-1])
    )


def _hash_stream(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _check_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise SnapshotExportError(f"destination is a symlink: {destination}")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SnapshotExportError(f"destination is not an empty folder: {destination}")


def _check_document(rel: str, data: bytes) -> None:
    """The publication policy for one exported file; see the module docstring."""
    text = _as_text(data)
    if text is None or "\x00" in text:
        raise SnapshotExportError(
            f"refusing to publish vault/{rel}: only UTF-8 text can be inspected for secrets"
            " (binary files such as images, PDFs, archives and databases are not published)"
        )
    if any(line.lstrip().startswith(ENCRYPTED_TEXT_MARKERS) for line in text.splitlines()):
        raise SnapshotExportError(
            f"refusing to publish vault/{rel}: it is an encrypted container that cannot be"
            " inspected"
        )
    findings = scan_text(text)
    if findings:
        raise SnapshotExportError(
            f"possible secret in vault/{rel} ({findings[0].masked()}"
            f"{f', {len(findings) - 1} more' if len(findings) > 1 else ''}); "
            "remove it and rotate the credential"
        )


def _stage(
    tar: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    staging: Path,
    expected_vault_id: uuid.UUID,
) -> tuple[_Manifest, int, int]:
    """Validates the archive and writes the exported vault files under `staging`.
    Returns (manifest, exported, excluded)."""
    files = _check_members(members)
    by_name = {m.name: m for m in members}
    manifest = _read_manifest(tar, by_name[MANIFEST])
    if manifest.vault_id != expected_vault_id:
        raise SnapshotExportError("archive is a backup of a different vault (vault_id mismatch)")
    present = files - {MANIFEST}
    if present != set(manifest.files):
        differ = sorted(present ^ set(manifest.files))
        raise SnapshotExportError(
            f"archive contents don't match MANIFEST.json "
            f"({len(differ)} files differ, e.g. {differ[0]!r})"
        )
    exported: set[str] = set()
    excluded = 0
    for member in members:
        if member.name not in present:
            continue
        parts = member.name.split("/")
        stream = tar.extractfile(member)
        if stream is None:
            raise SnapshotExportError(f"archive member is not readable: {member.name}")
        rel_parts = parts[1:]
        rel = "/".join(rel_parts)
        if parts[0] != "vault" or _is_excluded(rel_parts):
            if _hash_stream(stream) != manifest.files[member.name]:
                raise SnapshotExportError(f"checksum mismatch: {member.name}")
            if parts[0] == "vault":
                excluded += 1
            continue
        if _is_credential_path(rel_parts):
            raise SnapshotExportError(f"refusing to publish a credential file: vault/{rel}")
        if member.size >= FILE_LIMIT_BYTES:
            raise SnapshotExportError(f"vault/{rel} is 100 MiB or larger; GitHub rejects it")
        data = stream.read()
        if hashlib.sha256(data).hexdigest() != manifest.files[member.name]:
            raise SnapshotExportError(f"checksum mismatch: {member.name}")
        _check_document(rel, data)
        target = staging.joinpath(*rel_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(data)
        target.chmod(0o700 if member.mode & 0o100 else 0o600)
        exported.add(rel)
    if not exported:
        raise SnapshotExportError("archive has no vault documents to export")
    missing = [doc for doc in REQUIRED_DOCS if doc not in exported]
    if missing:
        raise SnapshotExportError(
            f"archive vault lacks {', '.join(missing)}; not a complete kit vault"
        )
    return manifest, len(exported), excluded


def export_snapshot(
    archive: Path, destination: Path, *, expected_vault_id: str
) -> dict[str, str | int]:
    """Writes the archive's canonical vault documents to `destination`, which must be absent
    or an empty folder. Nothing is written there unless the whole archive validates and
    belongs to the vault `expected_vault_id`."""
    expected = _parse_vault_id(expected_vault_id, "expected vault id")
    try:
        _check_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-export-", dir=destination.parent))
    except OSError as exc:
        raise SnapshotExportError(f"cannot prepare {destination}: {exc.strerror}") from None
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            manifest, exported, excluded = _stage(tar, members, staging, expected)
        _check_destination(destination)
        if destination.exists():
            destination.rmdir()
        staging.rename(destination)
    except (tarfile.TarError, EOFError, zlib.error) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotExportError(
            f"archive is unreadable or truncated ({type(exc).__name__})"
        ) from None
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        reason = exc.strerror or type(exc).__name__
        raise SnapshotExportError(f"cannot export {archive} to {destination}: {reason}") from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "vault_id": str(manifest.vault_id),
        "created_at": manifest.created_at,
        "files": exported,
        "excluded": excluded,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vault_watcher.snapshot_export",
        description="Export the canonical vault documents from a kit backup archive.",
    )
    parser.add_argument("--vault-id", required=True, help="UUID of the vault being published")
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        result = export_snapshot(args.archive, args.destination, expected_vault_id=args.vault_id)
    except SnapshotExportError as exc:
        print(f"snapshot export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
