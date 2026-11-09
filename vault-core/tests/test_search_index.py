import sqlite3
from pathlib import Path

import pytest

from vault_core.policy import Policy, ReadPolicy, WritePolicy
from vault_core.search_index import SearchIndex, search

POLICY = Policy(write=WritePolicy(allow=["wiki/"]), read=ReadPolicy(deny=["private/"]))


@pytest.fixture
def index(tmp_path: Path) -> SearchIndex:
    return SearchIndex(tmp_path / "index.db", POLICY)


def rows(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT path FROM docs ORDER BY path")]


def test_denied_hidden_and_non_markdown_files_are_never_indexed(
    index: SearchIndex, tmp_path: Path
) -> None:
    index.update("wiki/ok.md", b"# Ok\n")
    index.update("private/salaries.md", b"# Salaries\n")
    index.update(".obsidian/notes.md", b"# Hidden\n")
    index.update("attachments/scan.pdf", b"%PDF")
    assert rows(tmp_path / "index.db") == ["wiki/ok.md"]


def test_update_replaces_and_none_deletes(index: SearchIndex, tmp_path: Path) -> None:
    index.update("wiki/a.md", b"# Alpha\n\nfirst words\n")
    index.update("wiki/a.md", b"# Alpha\n\nsecond words\n")
    assert [h.path for h in search(tmp_path / "index.db", "second")] == ["wiki/a.md"]
    assert search(tmp_path / "index.db", "first") == []
    index.update("wiki/a.md", None)
    assert rows(tmp_path / "index.db") == []


def test_rebuild_indexes_the_whole_vault_and_drops_stale_rows(
    index: SearchIndex, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "private").mkdir()
    (vault / "wiki" / "a.md").write_text("# A\n")
    (vault / "private" / "p.md").write_text("# P\n")
    index.update("wiki/stale.md", b"# Stale\n")
    assert index.rebuild(vault) == 1
    assert rows(tmp_path / "index.db") == ["wiki/a.md"]


def test_title_comes_from_frontmatter_then_first_heading(
    index: SearchIndex, tmp_path: Path
) -> None:
    index.update("wiki/fm.md", b"---\ntitle: From Frontmatter\n---\n# Heading\n")
    index.update("wiki/h1.md", b"intro\n\n# From Heading\n\n## Sub\n")
    index.update("wiki/none.md", b"no headings at all\n")
    db = tmp_path / "index.db"
    with sqlite3.connect(db) as conn:
        got = dict(conn.execute("SELECT path, title FROM docs"))
    assert got == {
        "wiki/fm.md": "From Frontmatter",
        "wiki/h1.md": "From Heading",
        "wiki/none.md": "none",
    }


@pytest.mark.parametrize(
    "query", ['"unbalanced', "a AND", "NEAR(x y)", "col:value", "*", "-", "(", "it's", "ü"]
)
def test_fts_syntax_in_queries_never_errors(index: SearchIndex, tmp_path: Path, query: str) -> None:
    index.update("wiki/a.md", b"# A\n\nsome text\n")
    assert search(tmp_path / "index.db", query) == []


def test_operators_are_searched_as_plain_words(index: SearchIndex, tmp_path: Path) -> None:
    index.update("wiki/a.md", b"# A\n\ncats OR dogs\n")
    index.update("wiki/b.md", b"# B\n\ncats only\n")
    assert [h.path for h in search(tmp_path / "index.db", "cats OR dogs")] == ["wiki/a.md"]


def test_title_matches_rank_above_body_matches(index: SearchIndex, tmp_path: Path) -> None:
    index.update("wiki/body.md", b"# Something else\n\nThis page mentions tokenizer once.\n")
    index.update("wiki/title.md", b"# Tokenizer\n\nShort page.\n")
    assert [h.path for h in search(tmp_path / "index.db", "tokenizer")] == [
        "wiki/title.md",
        "wiki/body.md",
    ]


def test_accents_are_folded(index: SearchIndex, tmp_path: Path) -> None:
    index.update("wiki/es.md", b"# Canci\xc3\xb3n\n")
    assert [h.path for h in search(tmp_path / "index.db", "cancion")] == ["wiki/es.md"]


def test_search_filters_by_prefixes_in_sql(index: SearchIndex, tmp_path: Path) -> None:
    for p in ("wiki/a.md", "wiki/sub/b.md", "projects/c.md", "wiki_other/d.md"):
        index.update(p, b"# Shared term\n")
    db = tmp_path / "index.db"
    assert sorted(h.path for h in search(db, "shared", prefixes=["wiki/"])) == [
        "wiki/a.md",
        "wiki/sub/b.md",
    ]
    assert sorted(h.path for h in search(db, "shared", prefixes=["projects/", "wiki/sub/"])) == [
        "projects/c.md",
        "wiki/sub/b.md",
    ]
    assert len(search(db, "shared", limit=2)) == 2


def test_snippet_marks_the_match(index: SearchIndex, tmp_path: Path) -> None:
    index.update("wiki/a.md", b"# A\n\nThe watcher keeps history for every change.\n")
    (hit,) = search(tmp_path / "index.db", "history")
    assert "[history]" in hit.snippet
