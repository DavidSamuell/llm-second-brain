"""SQLite FTS5 search index at `<data>/index.db` (SPEC §7.2, plan D23).

vault-watcher owns writes; vault-mcp only queries, read-only. `read.deny`, hidden and
non-Markdown files are never indexed, so they can't leak through search.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vault_core import frontmatter
from vault_core.fsio import version_of
from vault_core.paths import is_hidden
from vault_core.policy import Policy, under

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    rowid INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    headings TEXT NOT NULL,
    frontmatter TEXT NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, headings, path, frontmatter, body,
    content='docs', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, headings, path, frontmatter, body)
    VALUES (new.rowid, new.title, new.headings, new.path, new.frontmatter, new.body);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, headings, path, frontmatter, body)
    VALUES ('delete', old.rowid, old.title, old.headings, old.path, old.frontmatter, old.body);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, headings, path, frontmatter, body)
    VALUES ('delete', old.rowid, old.title, old.headings, old.path, old.frontmatter, old.body);
    INSERT INTO docs_fts(rowid, title, headings, path, frontmatter, body)
    VALUES (new.rowid, new.title, new.headings, new.path, new.frontmatter, new.body);
END;
"""
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_WORDISH = re.compile(r"\w")
MAX_LIMIT = 100


@dataclass(frozen=True)
class SearchHit:
    path: str
    title: str
    snippet: str
    score: float


def _flatten(meta: dict[str, object] | None) -> str:
    if not meta:
        return ""
    lines = []
    for key, value in meta.items():
        text = " ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def _extract(rel: str, data: bytes) -> tuple[str, str, str, str]:
    text = data.decode("utf-8", errors="replace")
    meta, body = frontmatter.parse(text)
    headings = [m.group(1) for line in body.splitlines() if (m := _HEADING.match(line))]
    title = meta.get("title") if meta else None
    if not isinstance(title, str) or not title.strip():
        first_h1 = next(
            (line[2:].strip() for line in body.splitlines() if line.startswith("# ")), ""
        )
        title = first_h1 or Path(rel).stem
    return title, "\n".join(headings), _flatten(meta), body


class SearchIndex:
    def __init__(self, db_path: Path, policy: Policy | None) -> None:
        self.db_path = db_path
        self.policy = policy
        with self._db() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def indexable(self, rel: str) -> bool:
        if is_hidden(rel) or not rel.lower().endswith(".md"):
            return False
        return not (self.policy is not None and under(rel, self.policy.read.deny))

    def _upsert(self, conn: sqlite3.Connection, rel: str, data: bytes) -> None:
        title, headings, meta, body = _extract(rel, data)
        conn.execute(
            "INSERT INTO docs (path, title, headings, frontmatter, body, sha256) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET title=excluded.title, headings=excluded.headings,"
            " frontmatter=excluded.frontmatter, body=excluded.body, sha256=excluded.sha256",
            (rel, title, headings, meta, body, version_of(data)),
        )

    def update(self, rel: str, data: bytes | None) -> None:
        """Index `data` for `rel`, or remove it (None, or the path isn't indexable)."""
        with self._db() as conn:
            if data is None or not self.indexable(rel):
                conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
            else:
                self._upsert(conn, rel, data)

    def rebuild(self, vault_dir: Path) -> int:
        """Full rebuild from the vault (startup and `scripts/reindex`). Returns documents indexed."""
        root = vault_dir.resolve()
        count = 0
        with self._db() as conn:
            conn.execute("DELETE FROM docs")
            for path in sorted(vault_dir.rglob("*.md")):
                rel = path.relative_to(vault_dir).as_posix()
                if (
                    not path.is_file()
                    or not self.indexable(rel)
                    or not path.resolve().is_relative_to(root)
                ):
                    continue
                self._upsert(conn, rel, path.read_bytes())
                count += 1
        return count


def _match_expression(query: str) -> str | None:
    """Each whitespace-separated term becomes a quoted phrase, so no FTS5 syntax is possible.
    Terms without any word character are dropped. Terms are ANDed."""
    terms = [t for t in query.split() if _WORDISH.search(t)]
    if not terms:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(
    db_path: Path, query: str, *, prefixes: list[str] | None = None, limit: int = 20
) -> list[SearchHit]:
    """Query the index read-only. `prefixes` (OR-ed) narrow results to folders."""
    expression = _match_expression(query)
    if expression is None:
        return []
    # bm25 weights follow the FTS column order: title, headings, path, frontmatter, body.
    sql = (
        "SELECT d.path, d.title, snippet(docs_fts, 4, '[', ']', '…', 12),"
        " bm25(docs_fts, 10.0, 5.0, 3.0, 2.0, 1.0) AS rank"
        " FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid WHERE docs_fts MATCH ?"
    )
    params: list[object] = [expression]
    if prefixes is not None:
        if not prefixes:
            return []
        sql += " AND (" + " OR ".join("d.path LIKE ? ESCAPE '\\'" for _ in prefixes) + ")"
        params.extend(_like_prefix(p) for p in prefixes)
    sql += " ORDER BY rank LIMIT ?"
    params.append(max(1, min(limit, MAX_LIMIT)))
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [SearchHit(path=r[0], title=r[1], snippet=r[2], score=round(-r[3], 4)) for r in rows]
