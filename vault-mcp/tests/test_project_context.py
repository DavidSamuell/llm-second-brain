import json
from pathlib import Path

import pytest
import yaml

from vault_core.policy import Policy
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.tools_project import ProjectTools
from vault_watcher.graph_store import GraphStore


@pytest.fixture
def context_tools(
    vault: Path, data: Path, policy: Policy, store: CredentialStore
) -> tuple[ProjectTools, Credential, GraphStore]:
    files = {
        "projects/alpha.md": "---\ntype: project\ntitle: Alpha\nstatus: active\nrepo: [github.com/bookbot-hive/foo]\ngraph: {node_id: 'project:alpha', aliases: [Alpha project]}\n---\n# Alpha\n\n## Summary\nA training project.\n\n## Status\nShipping.\n\n## Change log\n- 2026-11-12: Oldest\n- 2026-11-13: Older\n- 2026-11-14: Middle\n- 2026-11-15: Recent\n- 2026-11-16: Newest\n",
        "projects/beta.md": "---\ntype: project\nrepo: [github.com/bookbot-hive/bar]\n---\n# Beta\n",
        "pipelines/etl.md": "---\ntype: pipeline\ntitle: ETL\nproject: [projects/alpha]\n---\n# ETL\n\n## Change log\n- 2026-11-17: Run on GPU2.\n",
        "_system/review-queue.md": "# Review\n- [ ] [[projects/alpha]] — Update status\n- [x] [[projects/alpha]] — Done\n- [ ] [[projects/beta]] — Unrelated\n- [ ] [[pipelines/etl]] — Clarify inputs\n- [ ] [[projects/alpha]] and [[private/salaries]] — PRIVATE DETAIL\n",
    }
    for rel, text in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    graph = GraphStore(vault, data, policy)
    graph.rebuild()
    _, cred = store.create_bearer(name="project-reader", owner="g@example.com", scope="read")
    return ProjectTools(vault, data, policy), cred, graph


def test_project_context_matches_ssh_remote_and_includes_relevant_current_information(
    context_tools,
) -> None:
    tools, cred, _ = context_tools
    result = tools.context(cred, repo="git@github.com:Bookbot-Hive/Foo.git")
    assert result["project"] == {
        "path": "projects/alpha.md",
        "title": "Alpha",
        "summary": "A training project.",
        "status": "Shipping.",
    }
    assert any(n["page"] == "pipelines/etl.md" for n in result["neighbors"]["nodes"])
    assert {e["predicate"] for e in result["neighbors"]["edges"]} >= {
        "belongs_to_project",
        "has_repo",
    }
    assert len(result["review_items"]) == 2
    assert "PRIVATE DETAIL" not in json.dumps(result)
    assert len(result["changes"]) == 5
    assert "GPU2" in result["changes"][0]
    assert "Oldest" not in str(result["changes"])
    assert len(json.dumps(result)) <= 4000


def test_project_context_unknown_ambiguous_and_conflicting_selectors_return_null(
    context_tools, vault: Path
) -> None:
    tools, cred, _ = context_tools
    assert tools.context(cred, repo="github.com/bookbot-hive/unknown") == {"project": None}
    assert tools.context(cred, repo="github.com/bookbot-hive/foo", project="projects/beta.md") == {
        "project": None
    }
    assert tools.context(cred) == {"project": None}
    (vault / "projects/duplicate.md").write_text(
        "---\ntype: project\nrepo: [github.com/bookbot-hive/foo]\n---\n"
    )
    assert tools.context(cred, repo="github.com/bookbot-hive/foo") == {"project": None}
    assert (
        tools.context(cred, repo="github.com/bookbot-hive/foo", project="projects/alpha.md")[
            "project"
        ]["title"]
        == "Alpha"
    )


def test_project_context_project_alias_and_canonical_path_both_work(context_tools) -> None:
    tools, cred, _ = context_tools
    for ref in ("project:alpha", "projects/alpha", "projects/alpha.md", "Alpha project"):
        assert tools.context(cred, project=ref)["project"]["path"] == "projects/alpha.md"


def test_project_context_never_leaks_neighbors_or_review_queue_outside_scope(
    context_tools, store: CredentialStore
) -> None:
    tools, _, _ = context_tools
    _, wiki = store.create_bearer(
        name="wiki-only-project", owner="g@example.com", scope="read", folders=["wiki/"]
    )
    assert tools.context(wiki, repo="github.com/bookbot-hive/foo") == {"project": None}
    _, projects = store.create_bearer(
        name="projects-only", owner="g@example.com", scope="read", folders=["projects/"]
    )
    result = tools.context(projects, project="project:alpha")
    assert result["review_items"] == []
    assert "pipelines/etl" not in json.dumps(result)
    assert all("GPU2" not in line for line in result["changes"])


def test_project_context_ignores_symlinked_and_hidden_project_pages(
    context_tools, vault: Path, tmp_path: Path
) -> None:
    tools, cred, _ = context_tools
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: project\nrepo: [github.com/bookbot-hive/secret]\n---\nSECRET\n")
    (vault / "projects/linked.md").symlink_to(outside)
    (vault / "projects/.hidden.md").write_text(outside.read_text())
    assert tools.context(cred, repo="github.com/bookbot-hive/secret") == {"project": None}


def test_project_context_budget_applies_to_serialized_unicode_and_escaped_text(
    context_tools, vault: Path
) -> None:
    tools, cred, graph = context_tools
    path = vault / "projects/alpha.md"
    meta = {
        "type": "project",
        "repo": ["github.com/bookbot-hive/foo"],
        "title": 'ü\\"' * 500,
        "graph": {"node_id": "project:alpha"},
    }
    text = (
        "---\n"
        + yaml.safe_dump(meta)
        + "---\n## Summary\n"
        + 'ü\\"' * 2000
        + "\n## Status\n"
        + "ü" * 1000
    )
    path.write_text(text)
    graph.update("projects/alpha.md", text.encode())
    result = tools.context(cred, repo="github.com/bookbot-hive/foo")
    assert result["project"]["path"] == "projects/alpha.md"
    assert result["project"]["summary"]
    assert result["project"]["status"]
    assert len(json.dumps(result)) <= 4000


@pytest.mark.parametrize("kwargs", [{"repo": "./local"}, {"project": ""}, {"repo": "x" * 4097}])
def test_project_context_rejects_invalid_arguments(context_tools, kwargs) -> None:
    from vault_mcp.errors import VaultToolError

    tools, cred, _ = context_tools
    with pytest.raises(VaultToolError) as exc:
        tools.context(cred, **kwargs)
    assert exc.value.code == "invalid"
