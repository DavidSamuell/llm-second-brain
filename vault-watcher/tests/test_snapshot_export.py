"""Snapshot export of kit backups for the private GitHub vault backup, unit level."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from vault_watcher import snapshot_export
from vault_watcher.backup import create_backup
from vault_watcher.snapshot_export import SnapshotExportError, export_snapshot, main

CREATED_AT = "2026-11-23T03:00:00Z"
VAULT_ID = "6f1c2a4e-8b3d-4f7a-9c21-0d5e6b7a8c9f"
OTHER_VAULT_ID = "0b7e9c3a-1d2f-4e5a-8b6c-7d8e9f0a1b2c"
GITHUB_TOKEN = "ghp_" + "A1b2" * 9  # matches the github-token rule; built so no literal sits here

Entry = tuple[tarfile.TarInfo, bytes]


def _file(name: str, body: bytes = b"# Page\n", mode: int = 0o644) -> Entry:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(body), mode
    return info, body


def _special(name: str, kind: bytes, linkname: str = "") -> Entry:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info, b""


MARKERS = [_file("vault/AGENTS.md", b"# Rules\n"), _file("vault/Home.md", b"# Home\n")]


def _manifest(files: dict[str, str], **extra: object) -> Entry:
    fields: dict[str, object] = {
        "format": 1,
        "created_at": CREATED_AT,
        "vault_id": VAULT_ID,
        "files": files,
    }
    body = json.dumps({**fields, **extra}).encode()
    return _file("MANIFEST.json", body)


def _archive(path: Path, entries: list[Entry], manifest: Entry | None = None) -> Path:
    """A gzipped tar of `entries`, with a correct MANIFEST.json unless one is given."""
    if manifest is None:
        manifest = _manifest(
            {i.name: hashlib.sha256(b).hexdigest() for i, b in entries if i.isreg()}
        )
    with tarfile.open(path, "w:gz") as tar:
        for info, body in [*entries, manifest]:
            tar.addfile(info, io.BytesIO(body) if info.isreg() else None)
    return path


def _export(archive: Path, dest: Path) -> dict[str, str | int]:
    return export_snapshot(archive, dest, expected_vault_id=VAULT_ID)


def _rewrite(src: Path, dst: Path, edit: Callable[[str, bytes], bytes]) -> Path:
    """Copies a backup archive member by member, letting `edit` change file bodies."""
    with tarfile.open(src) as old, tarfile.open(dst, "w:gz") as new:
        for member in old.getmembers():
            stream = old.extractfile(member)
            body = edit(member.name, stream.read()) if stream else b""
            member.size = len(body)
            new.addfile(member, io.BytesIO(body) if member.isreg() else None)
    return dst


def _read_manifest(archive: Path) -> dict[str, object]:
    with tarfile.open(archive) as tar:
        stream = tar.extractfile("MANIFEST.json")
        assert stream is not None
        manifest: dict[str, object] = json.loads(stream.read())
    return manifest


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _sqlite_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "scratch.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    db.commit()
    db.close()
    return path.read_bytes()


EXPORTED = {
    "AGENTS.md",
    "Home.md",
    ".gitignore",
    "wiki/page.md",
    "rules/style.md",
    "templates/meeting.md",
    "raw/source.txt",
    "attachments/diagram.svg",
    "skills/lint/run.sh",
}
EXCLUDED = {
    ".obsidian/workspace.json",
    ".obsidian/plugins/sync/data.json",
    ".obsidian/cache.db",
    ".trash/old.md",
    ".trash/.env",
    ".git/config",
    "projects/imported/.git/HEAD",
}


@pytest.fixture
def kit(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A live-looking vault and data folder, backed up as `scripts/backup` does."""
    vault, data, cfg = tmp_path / "vault", tmp_path / "data", tmp_path / "config"
    files: dict[str, bytes] = {
        "AGENTS.md": b"---\nschema_version: 1\n---\n# Rules\n",
        "Home.md": b"# Home\n",
        ".gitignore": b".obsidian/\n",
        "wiki/page.md": b"# Page\n\nSee [[other]].\n",
        "rules/style.md": b"# Style\n",
        "templates/meeting.md": b"# {{title}}\n",
        "raw/source.txt": b"pasted source\n",
        "attachments/diagram.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
        ".obsidian/workspace.json": b'{"token": "' + GITHUB_TOKEN.encode() + b'"}',
        ".obsidian/plugins/sync/data.json": b"{}",
        ".obsidian/cache.db": _sqlite_bytes(tmp_path),
        ".trash/old.md": b"# Deleted\n",
        ".trash/.env": b"API_KEY=whatever123\n",
        ".git/config": b"[core]\n",
        "projects/imported/.git/HEAD": b"ref: refs/heads/main\n",
    }
    for rel, body in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    script = vault / "skills" / "lint" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    (vault / "empty-folder").mkdir()
    (data / "locks").mkdir(parents=True)
    (data / "vault-id").write_text(VAULT_ID + "\n")  # the persisted id backups record
    (data / "tokens.db").write_bytes(_sqlite_bytes(tmp_path))
    (data / "ignis").mkdir()
    (data / "ignis" / "state.json").write_text('{"user": "settings"}')
    cfg.mkdir()
    (cfg / ".env").write_text("VAULT_MCP_TOKEN=secret-value-123\n")
    archive = tmp_path / "backups" / "kit.tar.gz"
    create_backup(vault, data, archive, [cfg / ".env"])
    return vault, data, archive


def _kit_vault_id(archive: Path) -> str:
    vault_id = _read_manifest(archive)["vault_id"]
    assert isinstance(vault_id, str)
    return vault_id


def test_exports_only_canonical_vault_documents(
    kit: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    vault, _, archive = kit
    manifest = _read_manifest(archive)
    vault_id = _kit_vault_id(archive)
    dest = tmp_path / "out" / "export" / "vault"
    result = export_snapshot(archive, dest, expected_vault_id=vault_id)

    assert result == {
        "vault_id": vault_id,
        "created_at": manifest["created_at"],
        "files": len(EXPORTED),
        "excluded": len(EXCLUDED),
    }
    listed = manifest["files"]
    assert isinstance(listed, dict)
    assert {n.removeprefix("vault/") for n in listed if n.startswith("vault/")} == (
        EXPORTED | EXCLUDED
    )
    tree = _tree(dest)
    assert set(tree) == EXPORTED  # no data/, config/, editor state, trash or git metadata
    assert all(tree[rel] == (vault / rel).read_bytes() for rel in EXPORTED)
    assert os.access(dest / "skills" / "lint" / "run.sh", os.X_OK)
    assert not os.access(dest / "wiki" / "page.md", os.X_OK)
    assert sorted(p.name for p in dest.parent.iterdir()) == ["vault"]  # no staging left behind


def test_export_is_deterministic(kit: tuple[Path, Path, Path], tmp_path: Path) -> None:
    _, _, archive = kit
    vault_id = _kit_vault_id(archive)
    first, second = tmp_path / "a", tmp_path / "b"
    second.mkdir()  # an existing empty folder is accepted too
    assert export_snapshot(archive, first, expected_vault_id=vault_id) == export_snapshot(
        archive, second, expected_vault_id=vault_id
    )
    assert _tree(first) == _tree(second)


def test_backup_of_another_vault_is_refused(kit: tuple[Path, Path, Path], tmp_path: Path) -> None:
    _, _, archive = kit
    other = OTHER_VAULT_ID if _kit_vault_id(archive) != OTHER_VAULT_ID else VAULT_ID
    dest = tmp_path / "out"
    with pytest.raises(SnapshotExportError, match="different vault"):
        export_snapshot(archive, dest, expected_vault_id=other)
    assert not dest.exists()


@pytest.mark.parametrize("member", ["vault/wiki/page.md", "data/ignis/state.json", "config/.env"])
def test_any_checksum_mismatch_refuses_without_writing(
    kit: tuple[Path, Path, Path], tmp_path: Path, member: str
) -> None:
    _, _, archive = kit
    tampered = _rewrite(
        archive, tmp_path / "t.tar.gz", lambda name, body: body + b"x" if name == member else body
    )
    dest = tmp_path / "out" / "vault"
    with pytest.raises(SnapshotExportError, match=f"checksum mismatch: {member}"):
        export_snapshot(tampered, dest, expected_vault_id=_kit_vault_id(archive))
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_manifest_and_members_must_agree(tmp_path: Path) -> None:
    page = _file("vault/page.md")
    extra = _archive(tmp_path / "extra.tar.gz", [page, _file("vault/unlisted.md")], _manifest(
        {"vault/page.md": hashlib.sha256(page[1]).hexdigest()}
    ))  # fmt: skip
    with pytest.raises(SnapshotExportError, match=r"1 files differ, e\.g\. 'vault/unlisted\.md'"):
        _export(extra, tmp_path / "a")
    missing = _archive(tmp_path / "missing.tar.gz", [page], _manifest(
        {"vault/page.md": hashlib.sha256(page[1]).hexdigest(), "vault/gone.md": "0" * 64}
    ))  # fmt: skip
    with pytest.raises(SnapshotExportError, match=r"'vault/gone\.md'"):
        _export(missing, tmp_path / "b")


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (_file("MANIFEST.json", b"not json"), "not valid JSON"),
        (_file("MANIFEST.json", b"[]"), "not an object"),
        (_manifest({}, format=2), "unsupported backup format"),
        (_manifest({}, format=True), "unsupported backup format"),
        (_manifest({}, created_at="yesterday"), "not a UTC timestamp"),
        (_manifest({"vault/page.md": "ABC"}), "invalid entry"),
        (_manifest({}, vault_schema_version="1"), "not an integer"),
        (_manifest({}, vault_id=None), "no vault_id"),
        (_manifest({}, vault_id="vault-1"), "vault_id is not a UUID"),
        (_manifest({}, vault_id=VAULT_ID.upper()), "vault_id is not a UUID in canonical form"),
        (_manifest({}, vault_id=OTHER_VAULT_ID), "different vault"),
    ],
)
def test_malformed_or_foreign_manifest_is_refused(
    tmp_path: Path, manifest: Entry, message: str
) -> None:
    archive = _archive(tmp_path / "a.tar.gz", [_file("vault/page.md")], manifest)
    with pytest.raises(SnapshotExportError, match=message):
        _export(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_archive_without_manifest_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info, body = _file("vault/page.md")
        tar.addfile(info, io.BytesIO(body))
    with pytest.raises(SnapshotExportError, match=r"MANIFEST\.json is missing"):
        _export(archive, tmp_path / "out")


@pytest.mark.parametrize("expected", ["", "not-a-uuid", VAULT_ID.replace("-", "")])
def test_expected_vault_id_must_be_a_canonical_uuid(tmp_path: Path, expected: str) -> None:
    archive = _archive(tmp_path / "a.tar.gz", MARKERS)
    with pytest.raises(SnapshotExportError, match="expected vault id is not a UUID"):
        export_snapshot(archive, tmp_path / "out", expected_vault_id=expected)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([_file("vault/../escape.md")], "unsafe member path"),
        ([_file("/etc/cron.d/x")], "unsafe member path"),
        ([_file("vault/./page.md")], "unsafe member path"),
        ([_file("vault/a\\..\\b.md")], "unsafe member path"),
        ([_file("vault/line\nbreak.md")], "unsafe member path"),
        ([_special("vault/link.md", tarfile.SYMTYPE, "/etc/passwd")], "not a regular file"),
        ([_file("vault/a.md"), _special("vault/b.md", tarfile.LNKTYPE, "vault/a.md")],
         "not a regular file"),
        ([_special("vault/fifo", tarfile.FIFOTYPE)], "not a regular file"),
        ([_special("vault/tty", tarfile.CHRTYPE)], "not a regular file"),
        ([_file("vault/page.md"), _file("vault/page.md", b"# Shadow\n")], "duplicate member"),
        ([_file("vault/page.md"), _file("vault/page.md/child.md")], "below a file"),
        ([_file("elsewhere/page.md")], "unexpected member"),
        ([_file("vault")], "unexpected member"),
    ],
)  # fmt: skip
def test_unsafe_archive_members_are_refused(
    tmp_path: Path, entries: list[Entry], message: str
) -> None:
    archive = _archive(tmp_path / "a.tar.gz", [*MARKERS, *entries])
    dest = tmp_path / "out" / "vault"
    with pytest.raises(SnapshotExportError, match=message):
        _export(archive, dest)
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []
    assert not (tmp_path / "escape.md").exists()


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        "projects/app/.env.production",
        "deploy/prod.env",
        ".credentials.json",
        "notes/credentials.json",
        "keys/id_ed25519",
        "certs/server.PEM",
        "backup/passwords.kdbx",
        "dotfiles/.ssh/config",
        ".git-credentials",
    ],
)
def test_credential_paths_are_refused(tmp_path: Path, rel: str) -> None:
    archive = _archive(tmp_path / "a.tar.gz", [*MARKERS, _file(f"vault/{rel}", b"harmless\n")])
    dest = tmp_path / "out"
    with pytest.raises(SnapshotExportError, match=f"credential file: vault/{rel}"):
        _export(archive, dest)
    assert not dest.exists()


def test_detected_secret_refuses_without_revealing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = f"# Setup\n\nexport GH={GITHUB_TOKEN}\n".encode()
    archive = _archive(tmp_path / "a.tar.gz", [*MARKERS, _file("vault/wiki/setup.md", body)])
    dest = tmp_path / "out"
    assert main(["--vault-id", VAULT_ID, str(archive), str(dest)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "possible secret in vault/wiki/setup.md (github-token at line 3)" in err
    assert GITHUB_TOKEN not in err
    assert not dest.exists()


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", f"token {GITHUB_TOKEN}\n")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        ("attachments/photo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(64)),
        ("attachments/scan.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"),
        ("attachments/bundle.zip", _zip_bytes()),
        ("attachments/notes.txt.gz", gzip.compress(f"token {GITHUB_TOKEN}\n".encode(), mtime=0)),
        ("attachments/cache.bin", b"SQLite format 3\x00" + bytes(84)),
        ("wiki/late-nul.md", b"# Page\n" + b"x" * 9000 + b"\x00"),
        ("wiki/latin1.md", b"caf\xe9\n"),
    ],
)
def test_binary_files_refuse_the_export_by_name(tmp_path: Path, rel: str, body: bytes) -> None:
    archive = _archive(tmp_path / "a.tar.gz", [*MARKERS, _file(f"vault/{rel}", body)])
    dest = tmp_path / "out"
    with pytest.raises(
        SnapshotExportError, match=f"refusing to publish vault/{rel}: only UTF-8 text"
    ):
        _export(archive, dest)
    assert not dest.exists()


@pytest.mark.parametrize(
    "body",
    [
        "-----BEGIN PGP MESSAGE-----\n\nhQEMA0xyz\n-----END PGP MESSAGE-----\n",
        "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24\n",
        "$ANSIBLE_VAULT;1.1;AES256\n6162636465666768\n",
    ],
)
def test_encrypted_text_containers_are_refused(tmp_path: Path, body: str) -> None:
    archive = _archive(tmp_path / "a.tar.gz", [*MARKERS, _file("vault/vault.txt", body.encode())])
    with pytest.raises(SnapshotExportError, match="encrypted container"):
        _export(archive, tmp_path / "out")


def test_excluded_and_unexported_files_may_be_binary(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "a.tar.gz",
        [
            *MARKERS,
            _file("vault/.obsidian/cache.db", b"SQLite format 3\x00" + bytes(84)),
            _file("data/tokens.db", b"SQLite format 3\x00" + bytes(84)),
        ],
    )
    result = _export(archive, tmp_path / "out")
    assert (result["files"], result["excluded"]) == (2, 1)
    assert set(_tree(tmp_path / "out")) == {"AGENTS.md", "Home.md"}


def test_files_of_github_size_limit_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshot_export, "FILE_LIMIT_BYTES", 64)
    under = _archive(tmp_path / "u.tar.gz", [*MARKERS, _file("vault/a.md", b"a" * 63)])
    assert _export(under, tmp_path / "u")["files"] == 3
    at = _archive(tmp_path / "a.tar.gz", [*MARKERS, _file("vault/a.md", b"a" * 64)])
    with pytest.raises(SnapshotExportError, match=r"vault/a\.md is 100 MiB or larger"):
        _export(at, tmp_path / "a")
    unexported = _archive(  # only exported files count: data/ may hold anything
        tmp_path / "e.tar.gz", [*MARKERS, _file("data/big", b"\x00" * 64)]
    )
    assert _export(unexported, tmp_path / "e")["files"] == 2


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([_file("vault/.obsidian/app.json", b"{}"), _file("data/x", b"1")], "no vault documents"),
        ([MARKERS[0], _file("vault/wiki/page.md")], "lacks Home.md"),
        ([MARKERS[1], _file("vault/wiki/page.md")], "lacks AGENTS.md"),
        ([_file("vault/wiki/AGENTS.md"), _file("vault/wiki/Home.md")], "lacks AGENTS.md, Home.md"),
    ],
)
def test_incomplete_vault_exports_are_refused(
    tmp_path: Path, entries: list[Entry], message: str
) -> None:
    archive = _archive(tmp_path / "a.tar.gz", entries)
    with pytest.raises(SnapshotExportError, match=message):
        _export(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_truncated_or_foreign_archives_are_refused(
    kit: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, _, archive = kit
    vault_id = _kit_vault_id(archive)
    truncated = tmp_path / "t.tar.gz"
    truncated.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])
    with pytest.raises(SnapshotExportError, match="unreadable or truncated"):
        export_snapshot(truncated, tmp_path / "a", expected_vault_id=vault_id)
    plain = tmp_path / "p.tar"
    with tarfile.open(plain, "w") as tar:
        info, body = _file("vault/page.md")
        tar.addfile(info, io.BytesIO(body))
    with pytest.raises(SnapshotExportError, match="unreadable or truncated"):
        export_snapshot(plain, tmp_path / "b", expected_vault_id=vault_id)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".snapshot")] == []


def test_destination_must_be_absent_or_an_empty_real_folder(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "a.tar.gz", MARKERS)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.md").write_text("mine")
    a_file = tmp_path / "file"
    a_file.write_text("mine")
    empty = tmp_path / "empty"
    empty.mkdir()
    link = tmp_path / "link"
    link.symlink_to(empty)
    for dest, message in ((busy, "not an empty folder"), (a_file, "not an empty folder"),
                          (link, "is a symlink")):  # fmt: skip
        with pytest.raises(SnapshotExportError, match=message):
            _export(archive, dest)
    assert (busy / "keep.md").read_text() == "mine"
    assert a_file.read_text() == "mine"
    assert list(empty.iterdir()) == []


def test_cli_requires_the_vault_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = _archive(tmp_path / "a.tar.gz", MARKERS)
    with pytest.raises(SystemExit) as exited:
        main([str(archive), str(tmp_path / "out")])
    assert exited.value.code == 2
    assert "--vault-id" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_module_cli_prints_the_result_as_json(kit: tuple[Path, Path, Path], tmp_path: Path) -> None:
    _, _, archive = kit
    vault_id = _kit_vault_id(archive)
    dest = tmp_path / "export" / "vault"
    command = [
        sys.executable, "-m", "vault_watcher.snapshot_export",
        "--vault-id", vault_id, str(archive), str(dest),
    ]  # fmt: skip
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result == {
        "vault_id": vault_id,
        "created_at": _read_manifest(archive)["created_at"],
        "files": len(EXPORTED),
        "excluded": len(EXCLUDED),
    }
    assert set(_tree(dest)) == EXPORTED

    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode == 1
    assert again.stdout == ""
    assert "snapshot export failed: destination is not an empty folder" in again.stderr
