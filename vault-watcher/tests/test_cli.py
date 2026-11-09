from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vault_core.history import HistoryStore
from vault_core.search_index import SearchIndex, search
from vault_watcher.cli import main

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # in the past: restores are stamped "now"
POLICY = """\
write: {allow: [wiki/]}
read: {deny: [private/]}
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    vault, data = tmp_path / "vault", tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    data.mkdir()
    policy = tmp_path / "vault-mcp.yaml"
    policy.write_text(POLICY)
    monkeypatch.setenv("VAULT_DIR", str(vault))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("POLICY_FILE", str(policy))
    return vault, data


def seed(data: Path, path: str, *contents: bytes | None) -> list[str]:
    store = HistoryStore(data)
    stamps = []
    for i, content in enumerate(contents):
        v = store.record(
            path,
            content,
            event="delete" if content is None else "modify",
            source="web",
            ts=T0 + timedelta(hours=i),
        )
        stamps.append(v.ts)
    return stamps


def test_list_shows_versions_oldest_first(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(env[1], "wiki/a.md", b"one", b"two", None)
    assert main(["history", "list", "wiki/a.md"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4  # header + three versions
    assert "delete" in lines[-1]
    assert "2026-01-01T12:00:00" in lines[1]


def test_restore_without_at_brings_back_the_last_content_and_records_it(
    env: tuple[Path, Path],
) -> None:
    vault, data = env
    seed(data, "wiki/a.md", b"# A\n\none", b"# A\n\ntwo keyword", None)
    assert main(["history", "restore", "wiki/a.md"]) == 0
    assert (vault / "wiki/a.md").read_bytes() == b"# A\n\ntwo keyword"
    head = HistoryStore(data).head("wiki/a.md")
    assert head is not None
    assert (head.kind, head.event, HistoryStore(data).content(head)) == (
        "restore",
        "create",
        b"# A\n\ntwo keyword",
    )
    assert [h.path for h in search(data / "index.db", "keyword")] == ["wiki/a.md"]


@pytest.mark.parametrize(
    ("at", "expected"), [("2026-01-01T12:30:00Z", b"one"), ("2026-01-01T13:00:00Z", b"two")]
)
def test_restore_at_picks_the_version_current_at_that_time(
    env: tuple[Path, Path], at: str, expected: bytes
) -> None:
    vault, data = env
    seed(data, "wiki/a.md", b"one", b"two", b"three")
    (vault / "wiki/a.md").write_bytes(b"three")
    assert main(["history", "restore", "wiki/a.md", "--at", at]) == 0
    assert (vault / "wiki/a.md").read_bytes() == expected


def test_restore_at_accepts_a_listed_version_timestamp(env: tuple[Path, Path]) -> None:
    vault, data = env
    stamps = seed(data, "wiki/a.md", b"one", b"two")
    assert main(["history", "restore", "wiki/a.md", "--at", stamps[0]]) == 0
    assert (vault / "wiki/a.md").read_bytes() == b"one"


def test_restore_deleted_restores_every_deleted_file_under_a_folder(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    vault, data = env
    seed(data, "wiki/x.md", b"x", None)
    seed(data, "wiki/sub/y.md", b"y", None)
    seed(data, "projects/z.md", b"z", None)
    seed(data, "wiki/alive.md", b"alive")
    (vault / "wiki/alive.md").write_bytes(b"alive")
    assert main(["history", "restore-deleted", "wiki/"]) == 0
    assert (vault / "wiki/x.md").read_bytes() == b"x"
    assert (vault / "wiki/sub/y.md").read_bytes() == b"y"
    assert not (vault / "projects/z.md").exists()
    out = capsys.readouterr().out
    assert "wiki/x.md" in out
    assert "wiki/sub/y.md" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["history", "restore", "wiki/never.md"], "no history"),
        (["history", "restore", "../etc/passwd"], "invalid"),
        (["history", "restore", "wiki/a.md", "--at", "yesterday"], "--at"),
        (["history", "restore", "wiki/a.md", "--at", "2025-01-01T00:00:00Z"], "no version"),
    ],
)
def test_restore_errors(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    seed(env[1], "wiki/a.md", b"one")
    assert main(argv) == 1
    assert message in capsys.readouterr().err


def test_reindex_rebuilds_from_the_vault(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    vault, data = env
    (vault / "wiki/a.md").write_text("# A\n\nzebra\n")
    (vault / "private").mkdir()
    (vault / "private/p.md").write_text("# P\n\nzebra\n")
    SearchIndex(data / "index.db", None).update("wiki/stale.md", b"# Stale\n\nzebra\n")
    assert main(["reindex"]) == 0
    assert [h.path for h in search(data / "index.db", "zebra")] == ["wiki/a.md"]
    assert "1" in capsys.readouterr().out
