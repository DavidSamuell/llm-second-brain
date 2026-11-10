"""The shipped vault template (SPEC §9, plan D36)."""

import json
from pathlib import Path

import pytest

from vault_core import frontmatter
from vault_core.template import (
    MANIFEST,
    is_managed,
    load_manifest,
    schema_version,
    updated_manifest,
)

TEMPLATE = Path(__file__).resolve().parents[2] / "vault-template"


def test_the_committed_manifest_matches_the_managed_files() -> None:
    """Editing a managed file without regenerating the manifest would make upgrade-vault treat
    kit content as a deployer customisation."""
    assert load_manifest(TEMPLATE) == updated_manifest(TEMPLATE), (
        "run: uv run python -m vault_core.template vault-template"
    )


def test_manifest_version_is_the_agents_md_schema_version() -> None:
    assert load_manifest(TEMPLATE)["schema_version"] == schema_version(TEMPLATE)


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
     ("Home.md", False), ("_system/index.md", False), ("wiki/concepts/x.md", False), (MANIFEST, False)],
)  # fmt: skip
def test_only_kit_content_is_managed(rel: str, managed: bool) -> None:
    assert is_managed(rel) is managed


@pytest.mark.parametrize("skill", ["docs-ingest", "docs-query", "docs-lint"])
def test_skills_are_valid_agent_skills(skill: str) -> None:
    """Agent Skills format: `name` matching the folder, and a `description` under 1024 chars."""
    meta, body = frontmatter.parse((TEMPLATE / "skills" / skill / "SKILL.md").read_text())
    assert meta is not None
    assert meta["name"] == skill
    assert 0 < len(meta["description"]) <= 1024
    assert body.strip()
