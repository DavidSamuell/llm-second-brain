import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vault_core import fsio
from vault_core.fsio import atomic_write, file_version, version_of


def test_version_is_lowercase_hex_sha256_of_raw_bytes() -> None:
    assert version_of(b"# Page\r\n") == hashlib.sha256(b"# Page\r\n").hexdigest()


def test_file_version_of_missing_file_is_none(tmp_path: Path) -> None:
    assert file_version(tmp_path / "missing.md") is None


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    atomic_write(target, b"one")
    atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert file_version(target) == version_of(b"two")


def test_atomic_write_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_bytes(b"old")
    target.chmod(0o640)
    atomic_write(target, b"new")
    assert target.stat().st_mode & 0o777 == 0o640


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    for i in range(3):
        atomic_write(target, f"v{i}".encode())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page.md"]


def test_failed_write_keeps_old_file_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "page.md"
    target.write_bytes(b"old")

    def broken_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fsio.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page.md"]


def test_process_killed_before_rename_keeps_old_file(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_bytes(b"old")
    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from vault_core import fsio
        target = Path(sys.argv[1])
        fsio.write_temp_sibling(target, b"new" * 100_000, tag="deadbeef")
        os._exit(9)  # killed between temp write and rename
        """
    )
    rc = subprocess.run([sys.executable, "-c", script, str(target)], check=False).returncode
    assert rc == 9
    assert target.read_bytes() == b"old"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "page.md"]
    assert leftovers == [".page.md.vault-mcp-deadbeef.tmp"]
    assert fsio.is_temp_name(leftovers[0])


def test_temp_names_are_hidden_dotfiles() -> None:
    assert fsio.temp_sibling(Path("/v/wiki/page.md"), "abc").name == ".page.md.vault-mcp-abc.tmp"
    assert not fsio.is_temp_name("page.md")
