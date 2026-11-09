"""vault-watcher against real directories and a real watchdog observer."""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from vault_core.audit import AuditLog, new_id
from vault_core.fsio import replace_from_temp, version_of, write_temp_sibling
from vault_core.history import HistoryStore, Spool
from vault_core.lock import vault_lock
from vault_watcher.watcher import Watcher, WatcherConfig


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool:
        self.sent.append((event, message))
        return True


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    vault, data = tmp_path / "vault", tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    data.mkdir()
    return vault, data


@pytest.fixture
def make_watcher(dirs: tuple[Path, Path]) -> Iterator[Callable[..., Watcher]]:
    started: list[Watcher] = []

    def make(**overrides: Any) -> Watcher:
        vault, data = dirs
        cfg = WatcherConfig(
            vault_dir=vault,
            data_dir=data,
            debounce_quiet_s=0.3,
            debounce_cap_s=5.0,
            scan_interval_s=3600,
            heartbeat_interval_s=0.1,
            **overrides,
        )
        w = Watcher(cfg, alerts=RecordingAlerts())
        w.start()
        started.append(w)
        return w

    yield make
    for w in started:
        w.stop()


def history(dirs: tuple[Path, Path]) -> HistoryStore:
    return HistoryStore(dirs[1])


def mcp_write(
    dirs: tuple[Path, Path], path: str, data: bytes, *, crash_after: str | None = None
) -> str:
    """vault-mcp's side of the D21 handoff, as T16 will implement it."""
    vault, data_dir = dirs
    spool, audit, hist = Spool(data_dir), AuditLog(data_dir / "audit.log"), HistoryStore(data_dir)
    target = vault / path
    audit_id = new_id()
    with vault_lock(data_dir, "vault-mcp"):
        txn = spool.prepare(audit_id, path, data, tool="write", credential_id="c1")
        tmp = write_temp_sibling(target, data, tag="abc123")
        current = target.read_bytes() if target.exists() else None
        head = hist.head(path)
        spool.write_pre(
            txn,
            current,
            record_pre=current is not None and (head is None or head.sha256 != version_of(current)),
        )
        if crash_after == "prepare":
            return audit_id
        replace_from_temp(tmp, target)
        if crash_after == "replace":
            return audit_id
        audit.append(service="vault-mcp", tool="write", result="ok", id=audit_id, path=path)
        if crash_after == "audit":
            return audit_id
        spool.commit(txn)
    return audit_id


def test_heartbeat_is_kept_fresh(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    make_watcher()
    beat = dirs[1] / "vault-watcher" / "heartbeat"
    wait_for(beat.exists)
    first = beat.stat().st_mtime
    wait_for(lambda: beat.stat().st_mtime > first)


def test_shell_edit_becomes_a_web_version_after_the_debounce(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    make_watcher()
    page = dirs[0] / "wiki" / "a.md"
    page.write_text("one")
    page.write_text("two")  # rapid saves coalesce
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    time.sleep(0.5)
    versions = history(dirs).versions("wiki/a.md")
    assert [(v.source, history(dirs).content(v)) for v in versions] == [("web", b"two")]


def test_committed_mcp_write_is_recorded_once_with_its_audit_id(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    (dirs[0] / "wiki" / "a.md").write_text("base")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    audit_id = mcp_write(dirs, "wiki/a.md", b"from agent")
    wait_for(lambda: not any(Spool(dirs[1]).inbox.iterdir()))  # acknowledged
    time.sleep(0.6)  # let the replace's fs event pass the debounce
    versions = history(dirs).versions("wiki/a.md")
    assert [(v.source, v.audit_id) for v in versions] == [("unknown", None), ("mcp", audit_id)]


def test_unrecorded_web_edit_before_an_mcp_write_is_kept(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    page = dirs[0] / "wiki" / "a.md"
    page.write_text("base")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    page.write_text("web edit inside the debounce window")
    mcp_write(dirs, "wiki/a.md", b"agent")
    wait_for(lambda: not any(Spool(dirs[1]).inbox.iterdir()))
    time.sleep(0.6)
    contents = [history(dirs).content(v) for v in history(dirs).versions("wiki/a.md")]
    assert contents == [b"base", b"web edit inside the debounce window", b"agent"]


def test_delete_creates_a_tombstone(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    page = dirs[0] / "wiki" / "a.md"
    page.write_text("x")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    page.unlink()
    wait_for(lambda: (h := history(dirs).head("wiki/a.md")) is not None and h.event == "delete")


def test_changes_made_while_stopped_are_caught_at_startup(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    page = dirs[0] / "wiki" / "a.md"
    page.write_text("v1")
    w = make_watcher()
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    w.stop()
    page.write_text("v2 while stopped")
    (dirs[0] / "wiki" / "new.md").write_text("new while stopped")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/new.md") is not None)
    head = history(dirs).head("wiki/a.md")
    assert head is not None
    assert (history(dirs).content(head), head.source) == (b"v2 while stopped", "unknown")


def test_ignored_paths_are_not_recorded(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    vault = dirs[0]
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.json").write_text("{}")
    (vault / ".git").mkdir()
    (vault / ".git" / "HEAD").write_text("ref")
    (vault / "wiki" / ".page.md.vault-mcp-abc.tmp").write_text("temp")
    (vault / "wiki" / "real.md").write_text("real")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/real.md") is not None)
    recorded = {
        str(p.parent.relative_to(dirs[1] / "history"))
        for p in (dirs[1] / "history").rglob("*.json")
        if ".objects" not in p.parts
    }
    assert recorded == {"wiki/real.md"}


def test_edits_while_git_sync_holds_the_lock_are_attributed_to_git(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    page = dirs[0] / "wiki" / "a.md"
    page.write_text("base")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/a.md") is not None)
    with vault_lock(dirs[1], "git-sync"):
        page.write_text("pulled from remote")
        time.sleep(0.2)
    wait_for(
        lambda: (
            (h := history(dirs).head("wiki/a.md")) is not None
            and h.sha256 == version_of(b"pulled from remote")
        )
    )
    head = history(dirs).head("wiki/a.md")
    assert head is not None
    assert head.source == "git"


# --- D21 crash recovery at startup ------------------------------------------------------------


def audit_rows(dirs: tuple[Path, Path], audit_id: str) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in (dirs[1] / "audit.log").read_text().splitlines()]
    return [r for r in rows if r.get("id") == audit_id]


@pytest.mark.parametrize(
    ("crash_after", "expected_file", "post_recorded", "audit_results"),
    [
        ("prepare", b"base", False, ["aborted"]),
        ("replace", b"agent", True, ["ok"]),  # repaired line: result ok, recovered
        ("audit", b"agent", True, ["ok"]),  # existing line, no duplicate
    ],
)
def test_prepared_entries_from_a_dead_vault_mcp_are_resolved(
    make_watcher: Callable[..., Watcher],
    dirs: tuple[Path, Path],
    crash_after: str,
    expected_file: bytes,
    post_recorded: bool,
    audit_results: list[str],
) -> None:
    (dirs[0] / "wiki" / "a.md").write_bytes(b"base")
    HistoryStore(dirs[1]).record("wiki/a.md", b"base", event="create", source="web")
    audit_id = mcp_write(dirs, "wiki/a.md", b"agent", crash_after=crash_after)
    make_watcher()
    wait_for(lambda: not any(Spool(dirs[1]).inbox.iterdir()))
    assert (dirs[0] / "wiki" / "a.md").read_bytes() == expected_file
    posts = [v for v in history(dirs).versions("wiki/a.md") if v.kind == "post"]
    assert bool(posts) is post_recorded
    rows = audit_rows(dirs, audit_id)
    assert [r["result"] for r in rows] == audit_results
    if crash_after == "replace":
        assert rows[0]["recovered"] is True
    assert not list((dirs[0] / "wiki").glob(".*.vault-mcp-*.tmp")), "temp file left behind"


def test_save_between_crash_and_recovery_is_unconfirmed_and_both_contents_kept(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    (dirs[0] / "wiki" / "a.md").write_bytes(b"base")
    HistoryStore(dirs[1]).record("wiki/a.md", b"base", event="create", source="web")
    audit_id = mcp_write(dirs, "wiki/a.md", b"agent", crash_after="replace")
    (dirs[0] / "wiki" / "a.md").write_bytes(b"saved in the browser after the crash")
    w = make_watcher()
    wait_for(lambda: not any(Spool(dirs[1]).inbox.iterdir()))
    versions = history(dirs).versions("wiki/a.md")
    contents = {history(dirs).content(v): v for v in versions}
    assert contents[b"agent"].unconfirmed is True
    assert contents[b"saved in the browser after the crash"].source == "unknown"
    assert [r["result"] for r in audit_rows(dirs, audit_id)] == ["unknown"]
    assert any(event == "history_unconfirmed" for event, _ in w.alerts.sent)  # type: ignore[attr-defined]


def test_symlinks_out_of_the_vault_are_never_copied_into_history(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside-secret.md"
    outside.write_text("db_password=not-for-history")
    (dirs[0] / "wiki" / "at-start.md").symlink_to(outside)
    (dirs[0] / "wiki" / "real.md").write_text("real")
    make_watcher()
    wait_for(lambda: history(dirs).head("wiki/real.md") is not None)
    (dirs[0] / "wiki" / "later.md").symlink_to(outside)
    (dirs[0] / "wiki" / "marker.md").write_text("after the symlink")
    wait_for(lambda: history(dirs).head("wiki/marker.md") is not None)
    assert history(dirs).head("wiki/at-start.md") is None
    assert history(dirs).head("wiki/later.md") is None
    blobs = [p.read_bytes() for p in (dirs[1] / "history" / ".objects").rglob("*") if p.is_file()]
    import gzip

    assert all(b"not-for-history" not in gzip.decompress(b) for b in blobs)


def test_edits_and_mcp_writes_are_searchable_and_deletes_drop_out(
    make_watcher: Callable[..., Watcher], dirs: tuple[Path, Path]
) -> None:
    from vault_core.search_index import search

    db = dirs[1] / "index.db"
    (dirs[0] / "wiki" / "existing.md").write_text("# Existing\n\nlighthouse\n")
    make_watcher()
    assert [h.path for h in search(db, "lighthouse")] == ["wiki/existing.md"]  # startup rebuild
    (dirs[0] / "wiki" / "web.md").write_text("# Web\n\nharbour\n")
    wait_for(lambda: [h.path for h in search(db, "harbour")] == ["wiki/web.md"])
    mcp_write(dirs, "wiki/agent.md", b"# Agent\n\nbeacon\n")
    wait_for(lambda: [h.path for h in search(db, "beacon")] == ["wiki/agent.md"])
    (dirs[0] / "wiki" / "web.md").unlink()
    wait_for(lambda: search(db, "harbour") == [])
