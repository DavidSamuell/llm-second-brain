"""scripts/upgrade-vault (SPEC §9.1, §16.22, plan D36)."""

import json
from pathlib import Path

import pytest

from vault_core.template import MANIFEST, updated_manifest
from vault_watcher.upgrade import apply, carry_custom_blocks, overridden, plan

AGENTS_V1 = """---
schema_version: 1
---
# Rules

<!-- custom -->
<!-- Add team-specific rules between these markers. -->
<!-- /custom -->

## Ingest

Old ingest steps.
"""
AGENTS_V2 = AGENTS_V1.replace("schema_version: 1", "schema_version: 2").replace(
    "Old ingest steps.", "New ingest steps."
)
TEMPLATE_V1 = "---\ntitle: ADR\nmaintainer: human\n---\n# {{title}}\n\n## Context\n"
TEMPLATE_V2 = TEMPLATE_V1 + "\n## Decision\n"


def kit(root: Path, files: dict[str, str], previous: dict[str, object] | None = None) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if previous is not None:
        (root / MANIFEST).write_text(json.dumps(previous))
    (root / MANIFEST).write_text(json.dumps(updated_manifest(root)))
    return root


@pytest.fixture
def kits(tmp_path: Path) -> tuple[Path, Path]:
    """Template v1 (installed earlier) and template v2 (the kit now), with a shared manifest."""
    v1 = kit(
        tmp_path / "kit-v1",
        {
            "AGENTS.md": AGENTS_V1,
            "_system/templates/adr.md": TEMPLATE_V1,
            "CLAUDE.md": "@AGENTS.md\n",
        },
    )
    previous = json.loads((v1 / MANIFEST).read_text())
    v2 = kit(tmp_path / "kit-v2", {"AGENTS.md": AGENTS_V2, "_system/templates/adr.md": TEMPLATE_V2,
                                   "CLAUDE.md": "@AGENTS.md\n", "_system/templates/guide.md": "# Guide\n"}, previous)  # fmt: skip
    return v1, v2


@pytest.fixture
def vault(tmp_path: Path, kits: tuple[Path, Path]) -> Path:
    v1, _ = kits
    vault = tmp_path / "vault"
    for path in v1.rglob("*"):
        if path.is_file() and path.name != MANIFEST:
            target = vault / path.relative_to(v1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    return vault


def test_16_22_custom_blocks_survive_and_kit_content_is_updated(
    vault: Path, kits: tuple[Path, Path], tmp_path: Path
) -> None:
    agents = vault / "AGENTS.md"
    agents.write_text(
        agents.read_text().replace(
            "<!-- Add team-specific rules between these markers. -->", "Always cite Jira tickets."
        )
    )
    p = plan(vault, kits[1])
    assert (p.vault_version, p.kit_version) == (1, 2)
    actions = {c.path: c.action for c in p.changes}
    assert actions == {
        "AGENTS.md": "update",
        "_system/templates/adr.md": "update",
        "_system/templates/guide.md": "add",
    }
    written = apply(p, vault, tmp_path / "data")
    assert sorted(written) == [
        "AGENTS.md",
        "_system/templates/adr.md",
        "_system/templates/guide.md",
    ]
    text = agents.read_text()
    assert "schema_version: 2" in text
    assert "New ingest steps." in text
    assert "<!-- custom -->\nAlways cite Jira tickets.\n<!-- /custom -->" in text
    assert plan(vault, kits[1]).changes == []  # idempotent


def test_files_changed_outside_custom_blocks_are_never_overwritten(
    vault: Path, kits: tuple[Path, Path], tmp_path: Path
) -> None:
    adr = vault / "_system" / "templates" / "adr.md"
    adr.write_text(adr.read_text() + "\n## Our own section\n")
    p = plan(vault, kits[1])
    change = next(c for c in p.changes if c.path == "_system/templates/adr.md")
    assert change.action == "customised"
    assert "+## Decision" in change.diff
    apply(p, vault, tmp_path / "data")
    assert "## Our own section" in adr.read_text()
    assert "## Decision" not in adr.read_text()


def test_vault_overrides_are_left_alone(
    vault: Path, kits: tuple[Path, Path], tmp_path: Path
) -> None:
    (vault / ".vault-overrides").write_text("# ours\n_system/templates/\n")
    p = plan(vault, kits[1])
    assert {c.path: c.action for c in p.changes if c.path.startswith("_system/templates/")} == {
        "_system/templates/adr.md": "override", "_system/templates/guide.md": "override",
    }  # fmt: skip
    apply(p, vault, tmp_path / "data")
    assert not (vault / "_system" / "templates" / "guide.md").exists()


def test_a_custom_block_whose_heading_disappeared_is_kept_at_the_end() -> None:
    old = "# A\n\n## Gone\n<!-- custom -->\nkeep me\n<!-- /custom -->\n"
    new = "# A\n\n## Other\n<!-- custom -->\n<!-- /custom -->\n"
    merged = carry_custom_blocks(old, new)
    assert merged.startswith(new)
    assert "Kept from your previous version (## Gone)" in merged
    assert "keep me" in merged


def test_blocks_are_matched_by_heading_not_position() -> None:
    old = "## One\n<!-- custom -->first<!-- /custom -->\n## Two\n<!-- custom -->second<!-- /custom -->\n"
    new = "## Two\n<!-- custom --><!-- /custom -->\n## One\n<!-- custom --><!-- /custom -->\n"
    assert (
        carry_custom_blocks(old, new)
        == "## Two\n<!-- custom -->second<!-- /custom -->\n## One\n<!-- custom -->first<!-- /custom -->\n"
    )


@pytest.mark.parametrize(
    ("rel", "patterns", "expected"),
    [
        ("AGENTS.md", ["AGENTS.md"], True),
        ("skills/docs-ingest/SKILL.md", ["skills/"], True),
        ("skills/docs-ingest/SKILL.md", ["SKILL.md"], True),  # no slash: any depth
        ("_system/templates/adr.md", ["/templates/"], False),  # anchored to the root
        (
            "_system/templates/adr.md",
            ["_system/templates/*.md", "!_system/templates/adr.md"],
            False,
        ),
        (
            "_system/templates/guide.md",
            ["_system/templates/*.md", "!_system/templates/adr.md"],
            True,
        ),
        ("skills/a/b/c.md", ["skills/**/c.md"], True),
        ("CLAUDE.md", ["AGENTS.md"], False),
    ],
)
def test_overrides_follow_gitignore_rules(rel: str, patterns: list[str], expected: bool) -> None:
    assert overridden(rel, patterns) is expected
