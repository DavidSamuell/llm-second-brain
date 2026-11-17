"""Permission-safe, rebuildable graph catalog. Markdown remains canonical."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vault_core import frontmatter
from vault_core.graph import clean_reference, default_ontology, extract, load_ontology, validate
from vault_core.paths import PathError, normalize, through_symlink
from vault_core.policy import Policy, check_read

if TYPE_CHECKING:
    from vault_watcher.recorder import AlertSink

ONTOLOGY_PATH = "_system/graph/ontology.yaml"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT NOT NULL, type TEXT NOT NULL, page TEXT PRIMARY KEY, title TEXT NOT NULL,
    source_summary INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nodes_id ON nodes(id);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT NOT NULL, id TEXT NOT NULL, page TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS aliases_page ON aliases(page);
CREATE TABLE IF NOT EXISTS node_refs (reference TEXT NOT NULL, page TEXT NOT NULL,
    PRIMARY KEY(reference, page));
CREATE INDEX IF NOT EXISTS node_refs_page ON node_refs(page);
CREATE TABLE IF NOT EXISTS edges (
    subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, page TEXT NOT NULL,
    source TEXT, evidence TEXT, confidence TEXT NOT NULL, status TEXT NOT NULL,
    explicit INTEGER NOT NULL, source_ref TEXT, field TEXT
);
CREATE INDEX IF NOT EXISTS edges_subject_predicate ON edges(subject, predicate);
CREATE INDEX IF NOT EXISTS edges_object_predicate ON edges(object, predicate);
CREATE INDEX IF NOT EXISTS edges_predicate_status ON edges(predicate, status);
CREATE INDEX IF NOT EXISTS edges_page ON edges(page);
CREATE INDEX IF NOT EXISTS edges_source_ref ON edges(source_ref);
CREATE TABLE IF NOT EXISTS errors (page TEXT NOT NULL, field TEXT NOT NULL,
    code TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS errors_page ON errors(page);
CREATE TABLE IF NOT EXISTS graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class GraphStore:
    def __init__(
        self, vault_dir: Path, data_dir: Path, policy: Policy, alerts: AlertSink | None = None
    ) -> None:
        self.vault_dir, self.data_dir, self.policy, self.alerts = (
            vault_dir,
            data_dir,
            policy,
            alerts,
        )
        self.db_path = data_dir / "graph.sqlite"
        self.ontology = default_ontology()
        self._ontology_stamp: object = object()
        self._needs_rebuild = False
        with self._db() as db:
            db.executescript(_SCHEMA)
            saved = db.execute("SELECT value FROM graph_meta WHERE key='ontology'").fetchone()
            if saved:
                self.ontology = load_ontology(saved[0])
            compiled = db.execute(
                "SELECT value FROM graph_meta WHERE key='compiled_ontology'"
            ).fetchone()
        self._reload_ontology()
        if compiled is not None and compiled[0] != self.ontology.model_dump_json():
            self._needs_rebuild = True

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def _reload_ontology(self) -> bool:
        path = self.vault_dir / ONTOLOGY_PATH
        try:
            # Never read an ontology through a symlink or a globally unreadable path.
            readable = check_read(ONTOLOGY_PATH, self.policy, None).allowed and not through_symlink(
                self.vault_dir, ONTOLOGY_PATH
            )
            stat = path.stat() if readable and path.is_file() else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat else None
            if stamp == self._ontology_stamp:
                return False
            self._ontology_stamp = stamp
            candidate = load_ontology(path.read_text()) if stat else default_ontology()
        except (ValueError, OSError):
            if self.alerts:
                self.alerts.send(
                    "graph_ontology_invalid",
                    "warning",
                    "Graph ontology is invalid; retaining the last valid ontology.",
                    {"path": ONTOLOGY_PATH},
                    subject=ONTOLOGY_PATH,
                )
            return False
        changed = candidate != self.ontology
        self.ontology = candidate
        self._needs_rebuild = self._needs_rebuild or changed
        with self._db() as db:
            db.execute(
                "INSERT INTO graph_meta(key,value) VALUES ('ontology',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (candidate.model_dump_json(),),
            )
        return changed

    def _indexable(self, rel: str) -> bool:
        try:
            normalize(rel)
        except PathError:
            return False
        return (
            rel.lower().endswith(".md")
            and check_read(rel, self.policy, None).allowed
            and not through_symlink(self.vault_dir, rel)
        )

    @staticmethod
    def _delete(db: sqlite3.Connection, rel: str) -> None:
        for query in (
            "DELETE FROM nodes WHERE page=?",
            "DELETE FROM aliases WHERE page=?",
            "DELETE FROM edges WHERE page=?",
            "DELETE FROM errors WHERE page=?",
            "DELETE FROM node_refs WHERE page=?",
        ):
            db.execute(query, (rel,))

    def _insert(self, db: sqlite3.Connection, rel: str, data: bytes) -> None:
        meta, body = frontmatter.parse(data.decode("utf-8", errors="replace"))
        graph = extract(rel, meta or {}, body, self.ontology)
        node = graph.node
        source_summary = int((meta or {}).get("type") in ("source", "source-summary"))
        db.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?)",
            (node.id, node.type, rel, node.title, source_summary),
        )
        db.executemany(
            "INSERT INTO aliases VALUES (?,?,?)", ((alias, node.id, rel) for alias in node.aliases)
        )
        refs = {
            clean_reference(ref)
            for ref in (rel, Path(rel).name, node.title, node.id, *node.aliases)
            if ref
        }
        db.executemany("INSERT INTO node_refs VALUES (?,?)", ((ref, rel) for ref in sorted(refs)))
        for edge in graph.edges:
            field = edge.field if edge.explicit else None
            db.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    edge.subject,
                    edge.predicate,
                    edge.object,
                    edge.page,
                    edge.source,
                    edge.evidence,
                    edge.confidence,
                    edge.status,
                    int(edge.explicit),
                    clean_reference(edge.source or ""),
                    field,
                ),
            )
        db.executemany(
            "INSERT INTO errors VALUES (?,?,?,?)",
            ((e.page, e.field, e.code, e.message) for e in validate(graph, self.ontology)),
        )

    @staticmethod
    def _source_errors(db: sqlite3.Connection) -> None:
        # Resolve from the current catalog, including ambiguity. Never infer a source's
        # type from its folder; changes to a source must revalidate its declaring pages.
        db.execute("DELETE FROM errors WHERE code='invalid_source'")
        db.execute("""INSERT INTO errors(page,field,code,message)
            SELECT e.page, e.field || '.source', 'invalid_source',
                   'source must resolve to a source-summary page'
            FROM edges e WHERE e.explicit=1 AND NOT EXISTS (
                SELECT 1 FROM node_refs r JOIN nodes n ON n.page=r.page
                WHERE r.reference=e.source_ref
                  AND (instr(e.source_ref, '/')=0 OR n.page=e.source_ref || '.md'
                       OR n.id=e.source_ref)
                GROUP BY r.reference HAVING count(*)=1 AND min(n.source_summary)=1
            )""")

    def update(self, rel: str, data: bytes | None) -> None:
        """Replace a page's derived state; ontology changes recompile the entire catalog."""
        self._reload_ontology()
        if self._needs_rebuild:
            self.rebuild()
        if rel == ONTOLOGY_PATH:
            return
        with self._db() as db:
            self._delete(db, rel)
            if data is not None and self._indexable(rel):
                self._insert(db, rel, data)
            self._source_errors(db)

    def rebuild(self) -> int:
        """Atomically replace all rows, keeping the database inode and existing readers."""
        self._reload_ontology()
        count = 0
        with self._db() as db:
            # Explicit BEGIN includes the empty-vault case in the same transaction.
            db.execute("BEGIN IMMEDIATE")
            for query in (
                "DELETE FROM nodes",
                "DELETE FROM aliases",
                "DELETE FROM edges",
                "DELETE FROM errors",
                "DELETE FROM node_refs",
            ):
                db.execute(query)
            for path in sorted(self.vault_dir.rglob("*")):
                rel = path.relative_to(self.vault_dir).as_posix()
                if not self._indexable(rel) or not path.is_file():
                    continue
                self._insert(db, rel, path.read_bytes())
                count += 1
            self._source_errors(db)
            db.execute(
                "INSERT INTO graph_meta(key,value) VALUES ('compiled_ontology',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.ontology.model_dump_json(),),
            )
        self._needs_rebuild = False
        return count
