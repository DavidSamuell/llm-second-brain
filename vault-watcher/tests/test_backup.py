"""Backup and restore (SPEC §16.16, plan D37), unit level."""

import io
import json
import sqlite3
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vault_watcher.backup import BackupError, create_backup, restore


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    vault, data = tmp_path / "vault", tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    (vault / "wiki" / "page.md").write_text("# Page\n")
    (vault / "AGENTS.md").write_text("---\nschema_version: 1\n---\n# Rules\n")
    (data / "locks").mkdir(parents=True)
    (data / "ignis").mkdir()
    (data / "ignis" / "state.json").write_text('{"user": "settings"}')
    (data / "ignis" / "plugins" / "headless-sync" / "ob-home").mkdir(
        parents=True
    )  # as Ignis leaves it
    db = sqlite3.connect(data / "tokens.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('committed in WAL')")
    db.commit()  # stays in the -wal file while the connection is open; a raw copy would miss it
    yield vault, data
    db.close()


def test_backup_then_restore_reproduces_vault_data_and_databases(
    site: tuple[Path, Path], tmp_path: Path
) -> None:
    vault, data = site
    archive = tmp_path / "out" / "backup.tar.gz"
    cfg = tmp_path / ".env"
    cfg.write_text("SECRET=1\n")
    manifest = create_backup(vault, data, archive, [cfg])
    assert manifest["vault_schema_version"] == 1
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "data/locks/vault.lock" not in names
    assert not [n for n in names if n.endswith(("-wal", "-shm"))]

    new_vault, new_data, restored_config = tmp_path / "v2", tmp_path / "d2", tmp_path / "cfg"
    new_vault.mkdir()
    new_data.mkdir()
    restore(archive, new_vault, new_data, restored_config)
    assert (new_vault / "wiki" / "page.md").read_text() == "# Page\n"
    assert (new_data / "ignis" / "state.json").read_text() == '{"user": "settings"}'
    assert (new_data / "ignis" / "plugins" / "headless-sync" / "ob-home").is_dir()
    rows = sqlite3.connect(new_data / "tokens.db").execute("SELECT v FROM t").fetchall()
    assert rows == [("committed in WAL",)]
    assert (restored_config / ".env").read_text() == "SECRET=1\n"


def test_restore_refuses_non_empty_targets_and_tampered_archives(
    site: tuple[Path, Path], tmp_path: Path
) -> None:
    vault, data = site
    archive = tmp_path / "b.tar.gz"
    create_backup(vault, data, archive)
    with pytest.raises(BackupError, match="not empty"):
        restore(archive, vault, data, tmp_path / "cfg")

    tampered = tmp_path / "t.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            f = src.extractfile(member)
            body = f.read() if f else b""
            if member.name == "vault/wiki/page.md":
                body = b"# Changed\n"
                member.size = len(body)
            dst.addfile(member, io.BytesIO(body))
    empty_v, empty_d = tmp_path / "ev", tmp_path / "ed"
    empty_v.mkdir()
    empty_d.mkdir()
    with pytest.raises(BackupError, match="checksum mismatch"):
        restore(tampered, empty_v, empty_d, tmp_path / "cfg")
    assert list(empty_v.iterdir()) == []


def test_force_replaces_existing_contents(site: tuple[Path, Path], tmp_path: Path) -> None:
    vault, data = site
    archive = tmp_path / "b.tar.gz"
    create_backup(vault, data, archive)
    (vault / "wiki" / "page.md").write_text("# Edited after the backup\n")
    (vault / "stray.md").write_text("x")
    restore(archive, vault, data, tmp_path / "cfg", force=True)
    assert (vault / "wiki" / "page.md").read_text() == "# Page\n"
    assert not (vault / "stray.md").exists()
    assert json.loads((data / "ignis" / "state.json").read_text()) == {"user": "settings"}
