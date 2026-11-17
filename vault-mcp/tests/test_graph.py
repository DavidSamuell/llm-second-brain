"""Read graph contracts, including real filesystem and SQLite permission boundaries."""

import json
import os
import sqlite3
from pathlib import Path
from time import perf_counter

import pytest

from vault_core.policy import Policy
from vault_mcp.errors import VaultToolError
from vault_mcp.store import CredentialStore
from vault_mcp.tools_graph import GraphTools

SCHEMA = """
CREATE TABLE nodes (id TEXT, type TEXT, page TEXT PRIMARY KEY, title TEXT);
CREATE TABLE aliases (alias TEXT, id TEXT, page TEXT);
CREATE TABLE edges (subject TEXT, predicate TEXT, object TEXT, page TEXT,
 source TEXT, evidence TEXT, confidence TEXT, status TEXT, explicit INTEGER);
CREATE TABLE errors (page TEXT, field TEXT, code TEXT, message TEXT);
CREATE INDEX edge_subject ON edges(subject);
CREATE INDEX edge_object ON edges(object);
"""


@pytest.fixture
def graph(vault: Path, data: Path, policy: Policy, store: CredentialStore):
    db = sqlite3.connect(data / "graph.sqlite")
    db.executescript(SCHEMA)
    _, cred = store.create_bearer(name="graph", owner="g@example.com", scope="read")
    _, scoped = store.create_bearer(
        name="scoped-graph", owner="g@example.com", scope="read", folders=["wiki/"]
    )

    def node(id, page, aliases=()):
        target = vault / page
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Graph page\n")
        db.execute("INSERT INTO nodes VALUES (?, ?, ?, ?)", (id, id.split(":")[0], page, id))
        db.executemany("INSERT INTO aliases VALUES (?, ?, ?)", [(a, id, page) for a in aliases])
        db.commit()

    def edge(
        subject, object, page, predicate="depends_on", source=None, evidence=None, explicit=True
    ):
        db.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, 'high', 'current', ?)",
            (subject, predicate, object, page, source, evidence, explicit),
        )
        db.commit()

    yield GraphTools(vault, data, policy), cred, scoped, node, edge, db
    db.close()


def test_graph_aliases_pages_implicit_links_and_all_five_tools(graph):
    tools, cred, _, node, edge, _ = graph
    node("project:a", "wiki/a.md", ["Alpha"])
    node("concept:b", "wiki/b.md")
    node("runbook:c", "wiki/c.md")
    edge("project:a", "page:wiki/b", "wiki/a.md", "mentions", explicit=False)
    edge("concept:b", "runbook:c", "wiki/b.md")
    neighbors = tools.neighbors(cred, "Alpha", depth=2)
    assert {n["id"] for n in neighbors["nodes"]} == {"project:a", "concept:b", "runbook:c"}
    assert any(e["explicit"] is False for e in neighbors["edges"])
    assert len(tools.edges(cred, subject="wiki/a.md", object="b")["edges"]) == 1
    assert tools.path(cred, "Alpha", "wiki/c.md")["paths"] == [
        ["project:a", "concept:b", "runbook:c"]
    ]
    assert tools.backlinks(cred, "wiki/b.md") == {"pages": ["wiki/a.md"]}
    assert tools.lint(cred) == {"errors": []}


def test_permissions_precede_limits_and_traversal_and_redact_evidence(graph):
    tools, cred, scoped, node, edge, _ = graph
    for id, page in [
        ("concept:a", "wiki/a.md"),
        ("concept:b", "wiki/b.md"),
        ("concept:private", "private/hidden.md"),
        ("concept:project", "projects/x.md"),
        ("concept:c", "wiki/c.md"),
    ]:
        node(id, page)
    edge("concept:a", "concept:private", "wiki/a.md")
    edge("concept:a", "concept:project", "wiki/a.md")
    edge("concept:a", "concept:b", "private/hidden.md")
    edge("concept:a", "concept:b", "wiki/a.md", source="private/hidden", evidence="TOP SECRET")
    edge("concept:private", "concept:c", "private/hidden.md")
    edge("concept:b", "concept:c", "projects/x.md")
    out = tools.edges(scoped, subject="concept:a", limit=1)
    assert len(out["edges"]) == 1
    assert out["edges"][0]["object"] == "concept:b"
    assert out["edges"][0]["source"] is None
    assert out["edges"][0]["evidence"] is None
    assert tools.path(scoped, "concept:a", "concept:c")["paths"] == []
    assert {n["id"] for n in tools.neighbors(scoped, "concept:a", depth=2)["nodes"]} == {
        "concept:a",
        "concept:b",
    }
    assert tools.neighbors(cred, "concept:private") == tools.neighbors(cred, "does-not-exist")
    assert "TOP SECRET" not in json.dumps(tools.lint(scoped))


def test_virtual_repo_requires_readable_declaration_and_raw_obeys_paths(graph, vault):
    tools, cred, scoped, node, edge, _ = graph
    node("project:a", "wiki/a.md")
    node("project:hidden", "private/hidden.md")
    edge("project:a", "repo:https://example.com/repo", "wiki/a.md", "has_repo", explicit=False)
    edge("project:hidden", "repo:hidden", "private/hidden.md", "has_repo", explicit=False)
    edge("project:a", "repo:orphan", "private/hidden.md")
    edge("project:a", "raw:private/raw.txt", "wiki/a.md", "summarizes_raw", explicit=False)
    edge("project:a", "raw:raw/public.txt", "wiki/a.md", "summarizes_raw", explicit=False)
    edge("project:a", "raw:../outside.txt", "wiki/a.md", "summarizes_raw", explicit=False)
    (vault / "raw").mkdir()
    (vault / "raw/public.txt").write_text("public")
    assert {e["object"] for e in tools.edges(cred)["edges"]} == {
        "repo:https://example.com/repo",
        "raw:raw/public.txt",
    }
    assert tools.neighbors(cred, "repo:hidden")["nodes"] == []
    assert tools.neighbors(cred, "repo:orphan")["nodes"] == []
    assert {e["object"] for e in tools.edges(scoped)["edges"]} == {"repo:https://example.com/repo"}


def test_duplicate_ids_and_aliases_are_ambiguous_and_exact_paths_never_fall_back(graph):
    tools, cred, _, node, edge, _ = graph
    node("concept:a", "wiki/a.md")
    node("concept:duplicate", "wiki/b.md", ["shared"])
    node("concept:duplicate", "wiki/c.md")
    node("concept:d", "wiki/other/b.md", ["shared"])
    edge("concept:a", "concept:duplicate", "wiki/a.md")
    edge("concept:a", "page:missing/b", "wiki/a.md", "mentions", explicit=False)
    edge("concept:a", "page:b", "wiki/a.md", "mentions", explicit=False)
    assert tools.edges(cred)["edges"] == []
    assert tools.neighbors(cred, "shared")["nodes"] == []
    assert tools.neighbors(cred, "wiki/b.md")["nodes"] == []
    assert {e["code"] for e in tools.lint(cred)["errors"]} >= {
        "duplicate_id",
        "duplicate_alias",
        "dangling_object",
    }


def test_lint_reports_safe_diagnostics_not_hidden_targets_or_stored_messages(graph):
    tools, cred, scoped, node, edge, db = graph
    node("concept:a", "wiki/a.md")
    node("concept:b", "wiki/b.md")
    node("concept:orphan", "wiki/orphan.md")
    node("concept:secret", "private/secret.md", ["secret-alias"])
    edge("concept:a", "concept:b", "wiki/a.md", "contradicts")
    edge("concept:a", "concept:missing", "wiki/a.md")
    edge("concept:a", "concept:secret", "wiki/a.md")
    db.execute(
        "INSERT INTO errors VALUES ('wiki/a.md', 'graph.relationships[0]', 'required', 'private/secret.md TOP SECRET')"
    )
    db.execute("INSERT INTO errors VALUES ('private/secret.md', 'graph', 'bad', 'TOP SECRET')")
    db.commit()
    result = tools.lint(scoped, folder="wiki")
    assert {e["code"] for e in result["errors"]} >= {
        "required",
        "dangling_object",
        "orphan_node",
        "contradicts",
    }
    assert "secret" not in json.dumps(result).lower()
    assert all(e["page"].startswith("wiki/") for e in result["errors"])
    assert tools.lint(cred, folder="projects")["errors"] == []


def test_hidden_symlink_and_scope_change_never_use_stale_permissions(graph, vault):
    tools, cred, scoped, node, edge, _ = graph
    node("concept:a", "wiki/a.md")
    node("concept:b", "wiki/b.md")
    node("concept:hidden", ".hidden/x.md")
    edge("concept:a", "concept:b", "wiki/a.md")
    edge("concept:a", "concept:hidden", "wiki/a.md")
    assert len(tools.edges(cred)["edges"]) == 1
    (vault / "wiki/b.md").unlink()
    (vault / "wiki/b.md").symlink_to(vault / "private/salaries.md")
    assert tools.edges(scoped)["edges"] == []


def test_implicit_resolution_tracks_new_target_and_target_id_changes(graph):
    tools, cred, _, node, edge, db = graph
    node("concept:a", "wiki/a.md")
    edge("concept:a", "page:wiki/b", "wiki/a.md", "mentions", explicit=False)
    assert tools.edges(cred)["edges"] == []
    node("concept:b", "wiki/b.md")
    assert tools.edges(cred)["edges"][0]["object"] == "concept:b"
    db.execute("UPDATE nodes SET id='concept:renamed' WHERE page='wiki/b.md'")
    db.commit()
    assert tools.edges(cred)["edges"][0]["object"] == "concept:renamed"


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("neighbors", {"node": "a", "depth": 3}),
        ("neighbors", {"node": "a", "direction": "sideways"}),
        ("edges", {"limit": 0}),
        ("edges", {"limit": True}),
        ("path", {"from_node": "a", "to_node": "b", "max_depth": 100}),
        ("backlinks", {"path": "../private/secret.md"}),
        ("lint", {"folder": "../private"}),
    ],
)
def test_graph_bad_parameters_are_invalid(graph, method, kwargs):
    tools, cred, *_ = graph
    with pytest.raises(VaultToolError) as exc:
        getattr(tools, method)(cred, **kwargs)
    assert exc.value.code == "invalid"


def test_empty_missing_and_corrupt_databases(vault, data, policy, store):
    _, cred = store.create_bearer(name="empty-graph", owner="g@example.com", scope="read")
    tools = GraphTools(vault, data, policy)
    assert tools.edges(cred) == {"edges": []}
    (data / "graph.sqlite").touch()
    assert tools.lint(cred) == {"errors": []}
    (data / "graph.sqlite").write_bytes(b"not a SQLite database")
    with pytest.raises(VaultToolError):
        tools.edges(cred)


def test_readable_evidence_is_retained_but_out_of_scope_source_is_redacted(graph):
    tools, cred, scoped, node, edge, _ = graph
    node("concept:a", "wiki/a.md")
    node("concept:b", "wiki/b.md")
    node("source:s", "projects/source.md")
    edge(
        "concept:a",
        "concept:b",
        "wiki/a.md",
        source="projects/source",
        evidence="Public project evidence",
    )
    unrestricted = tools.edges(cred)["edges"][0]
    assert unrestricted["source"] == "projects/source"
    assert unrestricted["evidence"] == "Public project evidence"
    restricted = tools.edges(scoped)["edges"][0]
    assert restricted["subject"] == "concept:a"
    assert restricted["object"] == "concept:b"
    assert restricted["source"] is None
    assert restricted["evidence"] is None


def test_hidden_alias_collisions_do_not_leak_diagnostics_or_choose_a_target(graph):
    tools, cred, _, node, edge, _ = graph
    node("concept:a", "wiki/a.md")
    node("concept:b", "wiki/b.md", ["shared"])
    node("concept:classified", "private/classified.md", ["shared"])
    edge("concept:a", "page:shared", "wiki/a.md", "mentions", explicit=False)
    assert tools.neighbors(cred, "shared") == {"nodes": [], "edges": []}
    assert tools.edges(cred)["edges"] == []
    result = tools.lint(cred)
    assert not any(e["code"] == "duplicate_alias" for e in result["errors"])
    assert "classified" not in json.dumps(result)


def test_direction_predicate_status_and_shortest_path_limits(graph):
    tools, cred, _, node, edge, db = graph
    for name in ("a", "b", "c"):
        node(f"concept:{name}", f"wiki/{name}.md")
    edge("concept:a", "concept:b", "wiki/a.md", "mentions", explicit=False)
    edge("concept:b", "concept:c", "wiki/b.md")
    edge("concept:a", "concept:c", "wiki/a.md")
    db.execute("UPDATE edges SET status='historical' WHERE predicate='mentions'")
    db.commit()
    assert tools.neighbors(cred, "concept:a", direction="in")["edges"] == []
    incoming = tools.neighbors(cred, "concept:b", direction="in", predicate="mentions")
    assert [e["subject"] for e in incoming["edges"]] == ["concept:a"]
    assert len(tools.edges(cred, status="historical")["edges"]) == 1
    assert tools.path(cred, "concept:a", "concept:c", max_depth=1)["paths"] == [
        ["concept:a", "concept:c"]
    ]
    assert tools.path(cred, "concept:c", "concept:a")["paths"] == []
    assert tools.path(cred, "concept:a", "concept:a")["paths"] == [["concept:a"]]


def test_graph_depth_two_performance_scenario(tmp_path, policy, store):
    """Opt-in proof: RUN_GRAPH_PERFORMANCE=1 pytest -k graph_depth_two_performance.

    Real 5k files / 50k stored edges; includes cold and warm query timings.
    This scenario is prepared for the integration owner, not run during implementation.
    """
    if os.environ.get("RUN_GRAPH_PERFORMANCE") != "1":
        pytest.skip("explicit performance proof scenario")
    vault, data = tmp_path / "perf-vault", tmp_path / "perf-data"
    (vault / "wiki").mkdir(parents=True)
    data.mkdir()
    db = sqlite3.connect(data / "graph.sqlite")
    db.executescript(SCHEMA)
    for i in range(5000):
        (vault / f"wiki/{i}.md").write_text("# Node\n")
    db.executemany(
        "INSERT INTO nodes VALUES (?, 'concept', ?, ?)",
        [(f"concept:{i}", f"wiki/{i}.md", str(i)) for i in range(5000)],
    )
    db.executemany(
        "INSERT INTO edges VALUES (?, 'mentions', ?, ?, NULL, NULL, 'low', 'current', 0)",
        [
            (f"concept:{i}", f"page:wiki/{(i + j) % 5000}", f"wiki/{i}.md")
            for i in range(5000)
            for j in range(1, 11)
        ],
    )
    db.commit()
    assert db.execute("SELECT count(*) FROM edges").fetchone()[0] == 50000
    db.close()
    _, cred = store.create_bearer(name="perf-graph", owner="g@example.com", scope="read")
    tools = GraphTools(vault, data, policy)
    timings = []
    for _ in range(2):
        start = perf_counter()
        result = tools.neighbors(cred, "concept:0", depth=2)
        timings.append(perf_counter() - start)
        assert len(result["edges"]) == 50
        assert len(result["nodes"]) > 10
    assert max(timings) < 0.2, {"cold_s": timings[0], "warm_s": timings[1]}
