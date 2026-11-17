"""The shipped vault template (SPEC §9, plan D36, SPEC-doc-authoring)."""

import json
import re
from pathlib import Path

import pytest
import yaml

from vault_core import frontmatter
from vault_core.graph import extract, load_ontology, validate
from vault_core.template import (
    MANIFEST,
    is_managed,
    load_manifest,
    schema_version,
    updated_manifest,
)

TEMPLATE = Path(__file__).resolve().parents[2] / "vault-template"
TEMPLATES = TEMPLATE / "_system" / "templates"
ONTOLOGY = load_ontology((TEMPLATE / "_system" / "graph" / "ontology.yaml").read_text())
HUMAN_DOC_TYPES = {
    "project", "product-spec", "architecture", "decision", "pipeline", "runbook", "guide",
}  # fmt: skip
# template -> (a page path following AGENTS.md section 2, its graph node type)
PLACES = {
    "project-overview": ("projects/alpha.md", "project"),
    "product-spec": ("specs/alpha/reader.md", "product-spec"),
    "architecture": ("specs/alpha/asr-architecture.md", "architecture"),
    "adr": ("specs/alpha/decisions/0001-use-gpu2.md", "decision"),
    "pipeline": ("pipelines/asr-training.md", "pipeline"),
    "runbook": ("runbooks/restart-asr.md", "runbook"),
    "guide": ("runbooks/onboarding.md", "guide"),
    "source-summary": ("wiki/sources/2026-11-17-asr-readme.md", "source"),
    "wiki-component": ("wiki/components/asr-model.md", "component"),
    "wiki-concept": ("wiki/concepts/search-ranking.md", "concept"),
    "glossary-term": ("wiki/glossary/wer.md", "glossary-term"),
    "wiki-team": ("wiki/people-teams/speech.md", "team"),
}


def rendered(name: str) -> str:
    text = (TEMPLATES / f"{name}.md").read_text()
    return text.replace("{{title}}", "Example").replace("{{date:YYYY-MM-DD}}", "2026-11-17")


def commented_graph(text: str) -> dict[str, object] | None:
    """The relationship example a template keeps as YAML comments in its frontmatter."""
    lines = text.split("---\n")[1].splitlines()
    if "# graph:" not in lines:
        return None
    block = [line[2:] for line in lines[lines.index("# graph:") :] if line.startswith("# ")]
    graph = yaml.safe_load("\n".join(block))["graph"]
    assert isinstance(graph, dict)
    return graph


def test_the_committed_manifest_matches_the_managed_files() -> None:
    """Editing a managed file without regenerating the manifest would make upgrade-vault treat
    kit content as a deployer customisation."""
    assert load_manifest(TEMPLATE) == updated_manifest(TEMPLATE), (
        "run: uv run python -m vault_core.template vault-template"
    )


def test_manifest_version_is_the_agents_md_schema_version() -> None:
    assert load_manifest(TEMPLATE)["schema_version"] == schema_version(TEMPLATE)


def test_the_manifest_keeps_every_released_version() -> None:
    manifest = load_manifest(TEMPLATE)
    current = schema_version(TEMPLATE)
    assert sorted(manifest["versions"], key=int) == [str(v) for v in range(1, current + 1)]
    assert "_system/graph/ontology.yaml" not in manifest["versions"]["1"]
    assert "_system/graph/ontology.yaml" in manifest["versions"]["2"]


def test_every_template_has_a_place_and_a_node_type() -> None:
    assert {p.stem for p in TEMPLATES.glob("*.md")} == set(PLACES)


@pytest.mark.parametrize("name", sorted(PLACES))
def test_a_page_from_each_template_has_a_valid_default_graph_node(name: str) -> None:
    path, node_type = PLACES[name]
    text = rendered(name)
    meta, body = frontmatter.parse(text)
    assert meta is not None
    page = extract(path, meta, body, ONTOLOGY)
    assert (page.node.type, page.node.id) == (node_type, f"{node_type}:{path.removesuffix('.md')}")
    assert validate(page, ONTOLOGY) == []
    headings = re.findall(r"^## (.+)$", body, re.MULTILINE)
    if node_type in HUMAN_DOC_TYPES:
        assert meta["maintainer"] == "shared"
        assert isinstance(meta["sources"], list)
        assert headings[-1] == "Change log"
    else:
        assert meta["maintainer"] == "agent"
    if node_type == "project":
        assert isinstance(meta["repo"], list) and isinstance(meta["owners"], list)
        assert headings == [
            "Summary", "Status", "Team", "Repositories", "Specs", "Pipelines", "Runbooks",
            "Decisions", "Change log",
        ]  # fmt: skip
    else:
        assert isinstance(meta["project"], list)


@pytest.mark.parametrize(
    "name", sorted(n for n in PLACES if commented_graph(rendered(n)) is not None)
)
def test_commented_relationship_examples_are_valid_once_uncommented(name: str) -> None:
    path, _ = PLACES[name]
    meta, body = frontmatter.parse(rendered(name))
    assert meta is not None
    meta["graph"] = commented_graph(rendered(name))
    page = extract(path, meta, body, ONTOLOGY)
    sources = {e.source: "source-summary" for e in page.edges if e.explicit and e.source}
    assert [e.predicate for e in page.edges if e.explicit]
    assert validate(page, ONTOLOGY, source_types=sources) == []


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in TEMPLATE.rglob("*.md")
        if p.read_text().startswith("---") and "skills" not in p.parts
    ),
)
def test_every_page_has_parseable_frontmatter_with_a_maintainer(path: Path) -> None:
    meta, _ = frontmatter.parse(path.read_text())
    assert meta is not None
    assert meta.get("maintainer") in ("human", "agent", "shared")


def test_obsidian_settings_are_valid_json() -> None:
    for name in ("app.json", "templates.json"):
        json.loads((TEMPLATE / ".obsidian" / name).read_text())


@pytest.mark.parametrize(
    ("rel", "managed"),
    [("AGENTS.md", True), ("_system/templates/adr.md", True), ("skills/docs-ingest/SKILL.md", True),
     ("_system/graph/ontology.yaml", True), ("_system/graph/other.yaml", False),
     ("Home.md", False), ("_system/index.md", False), ("wiki/concepts/x.md", False), (MANIFEST, False)],
)  # fmt: skip
def test_only_kit_content_is_managed(rel: str, managed: bool) -> None:
    assert is_managed(rel) is managed


@pytest.mark.parametrize("skill", ["docs-ingest", "docs-query", "docs-lint", "docs-capture"])
def test_skills_are_valid_agent_skills(skill: str) -> None:
    """Agent Skills format: `name` matching the folder, and a `description` under 1024 chars."""
    meta, body = frontmatter.parse((TEMPLATE / "skills" / skill / "SKILL.md").read_text())
    assert meta is not None
    assert meta["name"] == skill
    assert 0 < len(meta["description"]) <= 1024
    assert body.strip()
