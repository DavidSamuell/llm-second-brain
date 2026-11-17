import json
from pathlib import Path

import pytest
import yaml

from vault_core.policy import Policy
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.tools_project import ProjectTools
from vault_watcher.graph_store import GraphStore

ALPHA = "domains/asr/alpha/README.md"
BETA = "domains/tts/beta/README.md"
ETL = "domains/asr/shared/pipelines/etl.md"


@pytest.fixture
def context_tools(
    vault: Path, data: Path, policy: Policy, store: CredentialStore
) -> tuple[ProjectTools, Credential, GraphStore]:
    files = {
        ALPHA: "---\ntype: project\ntitle: Alpha\nstatus: active\nrepo: [github.com/bookbot-hive/foo]\ngraph: {node_id: 'project:alpha', aliases: [Alpha project]}\n---\n# Alpha\n\n## Summary\nA training project.\n\n## Status\nShipping.\n\n## Change log\n- 2026-11-12: Oldest\n- 2026-11-13: Older\n- 2026-11-14: Middle\n- 2026-11-15: Recent\n- 2026-11-16: Newest\n",
        BETA: "---\ntype: project\nrepo: [github.com/bookbot-hive/bar]\n---\n# Beta\n",
        ETL: "---\ntype: pipeline\ntitle: ETL\nproject: ['[[domains/asr/alpha/README]]']\n---\n# ETL\n\n## Change log\n- 2026-11-17: Run on GPU2.\n",
        "_system/review-queue.md": "# Review\n- [ ] [[domains/asr/alpha/README]] — Update status\n- [x] [[domains/asr/alpha/README]] — Done\n- [ ] [[domains/tts/beta/README]] — Unrelated\n- [ ] [[domains/asr/shared/pipelines/etl]] — Clarify inputs\n- [ ] [[domains/asr/alpha/README]] and [[private/salaries]] — PRIVATE DETAIL\n",
    }
    for rel, text in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    graph = GraphStore(vault, data, policy)
    graph.rebuild()
    _, cred = store.create_bearer(name="project-reader", owner="g@example.com", scope="read")
    return ProjectTools(vault, data, policy), cred, graph


def add_page(vault: Path, graph: GraphStore, rel: str, text: str) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    graph.update(rel, text.encode())


def test_project_context_matches_ssh_remote_and_includes_relevant_current_information(
    context_tools,
) -> None:
    tools, cred, _ = context_tools
    result = tools.context(cred, repo="git@github.com:Bookbot-Hive/Foo.git")
    assert result["project"] == {
        "path": ALPHA,
        "title": "Alpha",
        "summary": "A training project.",
        "status": "Shipping.",
    }
    assert any(n["page"] == ETL for n in result["neighbors"]["nodes"])
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
    assert tools.context(cred, repo="github.com/bookbot-hive/foo", project=BETA) == {
        "project": None
    }
    assert tools.context(cred) == {"project": None}
    duplicate = vault / "domains/asr/duplicate/README.md"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("---\ntype: project\nrepo: [github.com/bookbot-hive/foo]\n---\n")
    assert tools.context(cred, repo="github.com/bookbot-hive/foo") == {"project": None}
    assert (
        tools.context(cred, repo="github.com/bookbot-hive/foo", project=ALPHA)["project"]["title"]
        == "Alpha"
    )


def test_project_context_project_alias_and_canonical_path_both_work(context_tools) -> None:
    tools, cred, _ = context_tools
    for ref in ("project:alpha", "domains/asr/alpha/README", ALPHA, "Alpha project", "alpha"):
        assert tools.context(cred, project=ref)["project"]["path"] == ALPHA


def test_project_context_only_finds_overviews_two_levels_under_domains(
    context_tools, vault: Path
) -> None:
    """A `type: project` page anywhere else is never a project, even when it matches."""
    tools, cred, graph = context_tools
    stray = "---\ntype: project\ntitle: Alpha\nrepo: [github.com/bookbot-hive/foo]\n---\n# Stray\n"
    for rel in (
        "domains/asr/alpha/specs/x.md",
        "domains/asr/alpha/specs/README.md",
        "domains/asr/README.md",
        "domains/README.md",
        "projects/old.md",
        "shared/specs/README.md",
    ):
        add_page(vault, graph, rel, stray)
    assert tools.context(cred, repo="github.com/bookbot-hive/foo")["project"]["path"] == ALPHA
    assert tools.context(cred, project="Alpha")["project"]["path"] == ALPHA
    for rel in ("domains/asr/alpha/specs/x.md", "projects/old.md", "domains/asr/README.md"):
        assert tools.context(cred, project=rel) == {"project": None}


def test_project_context_selects_a_project_by_its_folder_name(context_tools, vault: Path) -> None:
    tools, cred, graph = context_tools
    rel = "domains/asr/parakeet/README.md"
    add_page(vault, graph, rel, "---\ntype: project\ntitle: Parakeet ASR\n---\n# Parakeet\n")
    assert tools.context(cred, project="parakeet")["project"]["path"] == rel


def test_project_context_readme_stem_is_never_a_selector(context_tools, vault: Path) -> None:
    tools, cred, _ = context_tools
    (vault / BETA).unlink()
    assert tools.context(cred, project="domains/asr/alpha/README")["project"]["path"] == ALPHA
    assert tools.context(cred, project="README") == {"project": None}
    assert tools.context(cred, repo="github.com/bookbot-hive/foo", project="README") == {
        "project": None
    }


def test_project_context_same_folder_name_in_two_domains_is_ambiguous(
    context_tools, vault: Path
) -> None:
    tools, cred, graph = context_tools
    other = "domains/tts/alpha/README.md"
    add_page(vault, graph, other, "---\ntype: project\ntitle: TTS Alpha\n---\n# TTS Alpha\n")
    assert tools.context(cred, project="alpha") == {"project": None}
    assert tools.context(cred, project=other)["project"]["path"] == other
    assert tools.context(cred, project="domains/tts/alpha/README")["project"]["path"] == other
    assert tools.context(cred, project=ALPHA)["project"]["path"] == ALPHA


def test_project_context_never_leaks_neighbors_or_review_queue_outside_scope(
    context_tools, store: CredentialStore
) -> None:
    tools, _, _ = context_tools
    _, wiki = store.create_bearer(
        name="wiki-only-project", owner="g@example.com", scope="read", folders=["wiki/"]
    )
    assert tools.context(wiki, repo="github.com/bookbot-hive/foo") == {"project": None}
    _, projects = store.create_bearer(
        name="projects-only", owner="g@example.com", scope="read", folders=["domains/asr/alpha/"]
    )
    result = tools.context(projects, project="project:alpha")
    assert result["project"]["path"] == ALPHA
    assert result["review_items"] == []
    assert "shared/pipelines/etl" not in json.dumps(result)
    assert all("GPU2" not in line for line in result["changes"])


def test_project_context_ignores_symlinked_and_hidden_project_pages(
    context_tools, vault: Path, tmp_path: Path
) -> None:
    tools, cred, _ = context_tools
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: project\nrepo: [github.com/bookbot-hive/secret]\n---\nSECRET\n")
    (vault / "domains/asr/linked").mkdir()
    (vault / "domains/asr/linked/README.md").symlink_to(outside)
    (vault / "domains/asr/.hidden").mkdir()
    (vault / "domains/asr/.hidden/README.md").write_text(outside.read_text())
    (vault / "domains/.hidden/secret").mkdir(parents=True)
    (vault / "domains/.hidden/secret/README.md").write_text(outside.read_text())
    (vault / "domains/asr/linked-dir").symlink_to(
        tmp_path / "outside-dir", target_is_directory=True
    )
    (tmp_path / "outside-dir").mkdir()
    (tmp_path / "outside-dir/README.md").write_text(outside.read_text())
    assert tools.context(cred, repo="github.com/bookbot-hive/secret") == {"project": None}


def test_project_context_budget_applies_to_serialized_unicode_and_escaped_text(
    context_tools, vault: Path
) -> None:
    tools, cred, graph = context_tools
    path = vault / ALPHA
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
    graph.update(ALPHA, text.encode())
    result = tools.context(cred, repo="github.com/bookbot-hive/foo")
    assert result["project"]["path"] == ALPHA
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
