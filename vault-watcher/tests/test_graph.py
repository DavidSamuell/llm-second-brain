"""Derived graph state and watcher integration, using real files and SQLite."""

import sqlite3
from pathlib import Path

import pytest

from vault_core.audit import AuditLog
from vault_core.graph import default_ontology
from vault_core.policy import Policy
from vault_watcher.cli import main
from vault_watcher.graph_store import GraphStore
from vault_watcher.recorder import Recorder
from vault_watcher.watcher import Watcher, WatcherConfig


class Alerts:
    def __init__(self) -> None:
        self.events: list[str] = []

    def send(self, event: str, *args: object, **kwargs: object) -> bool:
        self.events.append(event)
        return True


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    return GraphStore(
        vault,
        data,
        Policy.model_validate({"write": {"allow": ["wiki/"]}, "read": {"deny": ["private/"]}}),
    )


def put(store: GraphStore, rel: str, text: str) -> None:
    path = store.vault_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    store.update(rel, text.encode())


def state(store: GraphStore) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(store.db_path) as db:
        return {
            table: sorted(db.execute(query).fetchall(), key=repr)
            for table, query in (
                ("nodes", "SELECT * FROM nodes"),
                ("aliases", "SELECT * FROM aliases"),
                ("edges", "SELECT * FROM edges"),
                ("errors", "SELECT * FROM errors"),
                ("node_refs", "SELECT * FROM node_refs"),
            )
        }


def relationship(source: str) -> str:
    return f"""---
type: pipeline
graph:
  relationships:
    - predicate: produces
      object: component:model
      source: {source}
      evidence: Writes the model
      confidence: high
      status: current
---
[[wiki/target]]
"""


def test_graph_incremental_edit_rename_delete_equals_rebuild(store: GraphStore) -> None:
    put(store, "wiki/a.md", relationship("wiki/evidence"))
    put(store, "wiki/evidence.md", "---\ntype: source\n---\nEvidence")
    put(
        store,
        "wiki/target.md",
        "---\ntype: component\ngraph: {node_id: 'component:model', aliases: [Model]}\n---\n",
    )
    put(store, "wiki/a.md", relationship("wiki/evidence") + "[[Model]]")
    (store.vault_dir / "wiki/target.md").rename(store.vault_dir / "wiki/renamed.md")
    store.update("wiki/target.md", None)
    store.update("wiki/renamed.md", (store.vault_dir / "wiki/renamed.md").read_bytes())
    (store.vault_dir / "wiki/evidence.md").unlink()
    store.update("wiki/evidence.md", None)
    before = state(store)
    assert any(row[2] == "invalid_source" for row in before["errors"])
    assert store.rebuild() == 2
    assert state(store) == before


def test_graph_never_compiles_denied_hidden_or_symlink_content(
    store: GraphStore, tmp_path: Path
) -> None:
    put(store, "private/secret.md", "SECRET")
    put(store, ".hidden.md", "SECRET")
    put(store, "wiki/.hidden/file.md", "SECRET")
    put(store, "wiki/visible.md", "visible")
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET")
    (store.vault_dir / "wiki/link.md").symlink_to(outside)
    store.update("wiki/link.md", b"SECRET")
    # A previously readable page turned into a symlink must disappear too.
    (store.vault_dir / "wiki/visible.md").unlink()
    (store.vault_dir / "wiki/visible.md").symlink_to(outside)
    store.update("wiki/visible.md", b"SECRET")
    (store.vault_dir / "wiki/private-alias").symlink_to(
        store.vault_dir / "private", target_is_directory=True
    )
    store.update("wiki/private-alias/secret.md", b"SECRET")
    assert state(store)["nodes"] == []
    assert store.rebuild() == 0
    assert state(store)["nodes"] == []


def test_graph_source_validation_uses_metadata_and_revalidates_dependents(
    store: GraphStore,
) -> None:
    put(store, "wiki/sources/not-source.md", "---\ntype: concept\n---\n")
    put(store, "wiki/a.md", relationship("wiki/sources/not-source"))
    assert any(row[2] == "invalid_source" for row in state(store)["errors"])
    put(store, "wiki/sources/not-source.md", "---\ntype: source\n---\n")
    assert not state(store)["errors"]
    put(store, "wiki/sources/not-source.md", "---\ntype: source-summary\n---\n")
    assert not state(store)["errors"]
    put(
        store, "wiki/sources/not-source.md", "---\ntype: concept\ngraph: {node_type: source}\n---\n"
    )
    assert any(row[2] == "invalid_source" for row in state(store)["errors"])


def test_graph_ontology_non_markdown_change_recompiles_and_invalid_retains_last_good(
    store: GraphStore,
) -> None:
    alerts = Alerts()
    store.alerts = alerts
    put(store, "wiki/a.md", relationship("wiki/evidence"))
    put(store, "wiki/evidence.md", "---\ntype: source\n---\n")
    onto = default_ontology()
    del onto.predicates["produces"]
    put(store, "_system/graph/ontology.yaml", onto.model_dump_json())
    assert any(row[2] == "unknown_predicate" for row in state(store)["errors"])
    before = state(store)
    put(store, "_system/graph/ontology.yaml", "node_types: [broken")
    assert state(store) == before
    assert "graph_ontology_invalid" in alerts.events
    # A new store (e.g. CLI reindex) also keeps the persisted last valid ontology.
    restarted = GraphStore(store.vault_dir, store.data_dir, store.policy, alerts)
    restarted.rebuild()
    assert state(restarted) == before


def test_graph_recorder_updates_even_if_history_content_is_unchanged(store: GraphStore) -> None:
    alerts = Alerts()
    recorder = Recorder(
        store.vault_dir, store.data_dir, AuditLog(store.data_dir / "audit.log"), alerts, graph=store
    )
    put(store, "wiki/a.md", "[[Target]]")
    assert recorder.snapshot("wiki/a.md", "web")
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM nodes")
    assert not recorder.snapshot("wiki/a.md", "web")
    assert len(state(store)["nodes"]) == 1


def test_graph_reindex_cli_rebuilds_graph(
    store: GraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(store, "wiki/stale.md", "old")
    (store.vault_dir / "wiki/stale.md").unlink()
    (store.vault_dir / "wiki/new.md").write_text("[[Other]]")
    policy = tmp_path / "policy.yaml"
    policy.write_text("write: {allow: [wiki/]}\nread: {deny: [private/]}\n")
    monkeypatch.setenv("VAULT_DIR", str(store.vault_dir))
    monkeypatch.setenv("DATA_DIR", str(store.data_dir))
    monkeypatch.setenv("POLICY_FILE", str(policy))
    assert main(["reindex"]) == 0
    assert [row[2] for row in state(store)["nodes"]] == ["wiki/new.md"]


def test_graph_startup_rebuilds_changes_while_stopped(store: GraphStore) -> None:
    put(store, "wiki/stale.md", "old")
    (store.vault_dir / "wiki/stale.md").unlink()
    (store.vault_dir / "wiki/new.md").write_text("[[Other]]")
    watcher = Watcher(
        WatcherConfig(store.vault_dir, store.data_dir, policy=store.policy), alerts=Alerts()
    )
    try:
        watcher.start()
        assert [row[2] for row in state(store)["nodes"]] == ["wiki/new.md"]
    finally:
        watcher.stop()


def test_graph_rebuild_is_atomic_for_existing_readers(store: GraphStore) -> None:
    put(store, "wiki/old.md", "old")
    with sqlite3.connect(store.db_path) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT page FROM nodes").fetchall() == [("wiki/old.md",)]
        inode = store.db_path.stat().st_ino
        (store.vault_dir / "wiki/old.md").unlink()
        (store.vault_dir / "wiki/new.md").write_text("new")
        assert store.rebuild() == 1
        assert store.db_path.stat().st_ino == inode
        assert reader.execute("SELECT page FROM nodes").fetchall() == [("wiki/old.md",)]
        reader.commit()
        assert reader.execute("SELECT page FROM nodes").fetchall() == [("wiki/new.md",)]


def test_graph_failed_rebuild_rolls_back_all_rows(
    store: GraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(store, "wiki/a.md", "[[B]]")
    before = state(store)

    def fail(*args: object) -> None:
        raise OSError("interrupted read")

    monkeypatch.setattr(store, "_insert", fail)
    with pytest.raises(OSError, match="interrupted read"):
        store.rebuild()
    assert state(store) == before


def test_graph_recorder_detects_non_markdown_ontology_changes_and_deletion(
    store: GraphStore,
) -> None:
    alerts = Alerts()
    recorder = Recorder(
        store.vault_dir, store.data_dir, AuditLog(store.data_dir / "audit.log"), alerts, graph=store
    )
    put(store, "wiki/a.md", relationship("wiki/evidence"))
    put(store, "wiki/evidence.md", "---\ntype: source\n---\n")
    onto = default_ontology()
    del onto.predicates["produces"]
    path = store.vault_dir / "_system/graph/ontology.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(onto.model_dump_json())
    assert recorder.snapshot("_system/graph/ontology.yaml", "web")
    assert any(row[2] == "unknown_predicate" for row in state(store)["errors"])
    path.unlink()
    assert recorder.snapshot("_system/graph/ontology.yaml", "web")
    assert not state(store)["errors"]
    assert len(state(store)["nodes"]) == 2
