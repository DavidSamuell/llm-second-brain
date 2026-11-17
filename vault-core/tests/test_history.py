import gzip
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vault_core.history import (
    HistoryStore,
    Spool,
    classify,
    format_ts,
)

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "data")


def test_timestamps_are_sortable_utc(store: HistoryStore) -> None:
    assert format_ts(T0 + timedelta(microseconds=7)) == "20261109T120000.000007Z"
    v = store.record("wiki/a.md", b"x", event="create", source="web", ts=T0)
    assert re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z", v.ts)


def test_identical_content_is_stored_once(store: HistoryStore, tmp_path: Path) -> None:
    store.record("wiki/a.md", b"same", event="create", source="web", ts=T0)
    store.record("wiki/b.md", b"same", event="create", source="web", ts=T0)
    objects = [p for p in (tmp_path / "data/history/.objects").rglob("*") if p.is_file()]
    assert len(objects) == 1
    assert gzip.decompress(objects[0].read_bytes()) == b"same"


def test_versions_list_oldest_first_and_head_is_newest(store: HistoryStore) -> None:
    for i in range(3):
        store.record(
            "wiki/a.md",
            f"v{i}".encode(),
            event="modify",
            source="web",
            ts=T0 + timedelta(seconds=i),
        )
    versions = store.versions("wiki/a.md")
    assert [store.content(v) for v in versions] == [b"v0", b"v1", b"v2"]
    head = store.head("wiki/a.md")
    assert head is not None
    assert store.content(head) == b"v2"
    assert store.head("wiki/missing.md") is None


def test_same_timestamp_does_not_overwrite(store: HistoryStore) -> None:
    store.record("wiki/a.md", b"one", event="modify", source="web", ts=T0)
    store.record("wiki/a.md", b"two", event="modify", source="web", ts=T0)
    assert [store.content(v) for v in store.versions("wiki/a.md")] == [b"one", b"two"]


def test_record_is_idempotent_on_audit_id_and_kind(store: HistoryStore) -> None:
    for _ in range(2):
        store.record(
            "wiki/a.md", b"x", event="modify", source="mcp", ts=T0, audit_id="A1", kind="post"
        )
    store.record("wiki/a.md", b"w", event="modify", source="web", ts=T0, audit_id="A1", kind="pre")
    assert [(v.kind, v.audit_id) for v in store.versions("wiki/a.md")] == [
        ("post", "A1"),
        ("pre", "A1"),
    ]


def test_content_at_picks_the_version_current_at_that_time(store: HistoryStore) -> None:
    store.record("wiki/a.md", b"v0", event="create", source="web", ts=T0)
    store.record("wiki/a.md", b"v1", event="modify", source="web", ts=T0 + timedelta(hours=1))
    assert store.content_at("wiki/a.md", T0 + timedelta(minutes=30)) == b"v0"
    assert store.content_at("wiki/a.md", T0 + timedelta(hours=2)) == b"v1"
    assert store.content_at("wiki/a.md", T0 - timedelta(seconds=1)) is None


def test_tombstones_let_deleted_files_be_restored(store: HistoryStore) -> None:
    store.record("wiki/gone.md", b"last words", event="create", source="web", ts=T0)
    store.record("wiki/gone.md", None, event="delete", source="web", ts=T0 + timedelta(minutes=1))
    store.record("wiki/kept.md", b"here", event="create", source="web", ts=T0)
    store.record("projects/other.md", b"x", event="create", source="web", ts=T0)
    store.record(
        "projects/other.md", None, event="delete", source="web", ts=T0 + timedelta(minutes=1)
    )
    head = store.head("wiki/gone.md")
    assert head is not None
    assert head.event == "delete"
    assert store.deleted_under("wiki/") == {"wiki/gone.md": b"last words"}
    assert set(store.deleted_under("")) == {"wiki/gone.md", "projects/other.md"}


def test_gc_keeps_min_versions_regardless_of_age(store: HistoryStore, tmp_path: Path) -> None:
    for i in range(12):
        store.record(
            "wiki/a.md",
            f"old{i}".encode(),
            event="modify",
            source="web",
            ts=T0 + timedelta(seconds=i),
        )
    removed = store.gc(now=T0 + timedelta(days=400), retention_days=90, min_versions=10)
    assert removed == 2
    assert [store.content(v) for v in store.versions("wiki/a.md")] == [
        f"old{i}".encode() for i in range(2, 12)
    ]


def test_gc_drops_old_versions_beyond_min_and_sweeps_unreferenced_blobs(
    store: HistoryStore, tmp_path: Path
) -> None:
    for i in range(5):
        store.record(
            "wiki/a.md", f"v{i}".encode(), event="modify", source="web", ts=T0 + timedelta(days=i)
        )
    spool = Spool(tmp_path / "data")
    txn = spool.prepare("AUD1", "wiki/b.md", b"in flight", tool="write", credential_id="c")
    store.gc(now=T0 + timedelta(days=95), retention_days=90, min_versions=2)
    assert [store.content(v) for v in store.versions("wiki/a.md")] == [b"v3", b"v4"]
    objects = [p for p in (tmp_path / "data/history/.objects").rglob("*") if p.is_file()]
    assert len(objects) == 2
    assert (txn / "post.blob").read_bytes() == b"in flight"  # the spool is never collected


# --- spool / D21 transactions -------------------------------------------------------------


def test_committed_entry_records_pre_and_post_once(store: HistoryStore, tmp_path: Path) -> None:
    spool = Spool(tmp_path / "data")
    txn = spool.prepare("AUD1", "wiki/a.md", b"new", tool="write", credential_id="c")
    spool.write_pre(txn, b"unrecorded web edit", record_pre=True)
    spool.commit(txn)
    for _ in range(2):  # consuming twice must not duplicate pointers
        for entry in spool.entries():
            store.record_entry(entry)
    versions = store.versions("wiki/a.md")
    assert [(v.kind, v.source, store.content(v)) for v in versions] == [
        ("pre", "web", b"unrecorded web edit"),
        ("post", "mcp", b"new"),
    ]
    assert versions[1].audit_id == "AUD1"


def test_prepared_entries_are_not_committed_entries(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "data")
    spool.prepare("AUD1", "wiki/a.md", b"new", tool="write", credential_id="c")
    (entry,) = spool.entries()
    assert entry.state == "prepared"


def test_partial_entry_without_txn_json_is_ignored(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "data")
    partial = tmp_path / "data/history/.inbox/AUD9.prepared"
    partial.mkdir(parents=True)
    (partial / "post.blob").write_bytes(b"half")
    assert spool.entries() == []


def test_entry_merges_pre_json_into_txn(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "data")
    txn = spool.prepare("AUD1", "wiki/a.md", b"new", tool="write", credential_id="c")
    spool.write_pre(txn, b"old", record_pre=False)
    (entry,) = spool.entries()
    assert entry.pre_sha256 is not None
    assert entry.event == "modify"
    assert entry.has_pre is False


def test_drop_removes_the_entry(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "data")
    txn = spool.prepare("AUD1", "wiki/a.md", b"new", tool="write", credential_id="c")
    spool.drop(txn)
    assert spool.entries() == []


# --- D21 recovery classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "pre", "post", "pre_known", "expected"),
    [
        ("P", "O", "P", True, "applied"),
        ("O", "O", "P", True, "aborted"),
        (None, None, "P", True, "aborted"),  # create that never landed
        ("X", "O", "P", True, "unconfirmed"),  # someone saved after the crash
        ("P", None, "P", False, "applied"),  # pre.json lost (power loss): post still decides
        ("O", None, "P", False, "unconfirmed"),  # cannot tell aborted from overwritten
        (
            None,
            None,
            "P",
            False,
            "unconfirmed",
        ),  # absent file, pre unknown: create or later delete?
    ],
)
def test_classify(
    current: str | None, pre: str | None, post: str, pre_known: bool, expected: str
) -> None:
    assert classify(current, pre_sha256=pre, post_sha256=post, pre_known=pre_known) == expected
