import json
from pathlib import Path

import pytest
import yaml
from fastmcp.exceptions import ToolError

from vault_core import frontmatter
from vault_core.fsio import version_of
from vault_core.policy import Policy
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.writer import VaultWriter


@pytest.fixture
def graph_writer(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> tuple[VaultWriter, Credential]:
    source = vault / "wiki/sources/readme.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("---\ntype: source-summary\n---\nThe pipeline produces a model.\n")
    _, cred = store.create_bearer(name="graph-writer", owner="e2e@example.com", scope="write")
    return VaultWriter(vault, data, policy), cred


def content(**changes: str) -> str:
    edge = {
        "predicate": "produces",
        "object": "component:wiki/model",
        "source": "wiki/sources/readme",
        "evidence": "The pipeline produces a model.",
        "confidence": "high",
        "status": "current",
        **changes,
    }
    return (
        "---\n"
        + yaml.safe_dump({"type": "pipeline", "graph": {"relationships": [edge]}})
        + "---\n# Pipeline\n"
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"predicate": "made_up"}, "predicate"),
        ({"evidence": ""}, "evidence"),
        ({"object": "person:alice"}, "object"),
        ({"source": "wiki/concepts/search"}, "source"),
        ({"source": "private/salaries"}, "source"),
    ],
)
async def test_graph_invalid_write_changes_no_content(
    graph_writer: tuple[VaultWriter, Credential], changes: dict[str, str], field: str
) -> None:
    writer, cred = graph_writer
    with pytest.raises(ToolError) as exc:
        await writer.write(cred, "wiki/train.md", content(**changes), None)
    error = json.loads(str(exc.value))
    assert error["code"] == "invalid"
    assert any(e["field"].endswith("." + field) for e in error["details"]["graph_errors"])
    assert not (writer.vault_dir / "wiki/train.md").exists()


async def test_graph_valid_write_allows_dangling_target(
    graph_writer: tuple[VaultWriter, Credential],
) -> None:
    writer, cred = graph_writer
    await writer.write(cred, "wiki/train.md", content(), None)
    assert (writer.vault_dir / "wiki/train.md").read_text() == content()


async def test_graph_node_type_override_cannot_fake_source_summary(
    graph_writer: tuple[VaultWriter, Credential],
) -> None:
    writer, cred = graph_writer
    fake = writer.vault_dir / "wiki/not-source.md"
    fake.write_text("---\ntype: project\ngraph:\n  node_type: source\n---\nNot a source.\n")
    with pytest.raises(ToolError) as exc:
        await writer.write(cred, "wiki/train.md", content(source="wiki/not-source"), None)
    assert any(
        e["code"] == "invalid_source" for e in json.loads(str(exc.value))["details"]["graph_errors"]
    )


async def test_graph_move_rewrites_derived_ids_and_source_paths_without_changing_evidence(
    graph_writer: tuple[VaultWriter, Credential],
) -> None:
    writer, cred = graph_writer
    model = await writer.write(cred, "wiki/model.md", "---\ntype: component\n---\n# Model\n", None)
    original = content(
        evidence="An exact quote containing component:wiki/model must stay unchanged."
    )
    await writer.write(cred, "wiki/train.md", original, None)
    await writer.move(cred, "wiki/model.md", "wiki/new-model.md", model["version"])
    meta, body = frontmatter.parse((writer.vault_dir / "wiki/train.md").read_text())
    assert meta is not None
    edge = meta["graph"]["relationships"][0]
    assert edge["object"] == "component:wiki/new-model"
    assert edge["evidence"] == "An exact quote containing component:wiki/model must stay unchanged."
    assert body == "# Pipeline\n"
    source = writer.vault_dir / "wiki/sources/readme.md"
    await writer.move(
        cred, "wiki/sources/readme.md", "wiki/sources/renamed.md", version_of(source.read_bytes())
    )
    updated, _ = frontmatter.parse((writer.vault_dir / "wiki/train.md").read_text())
    assert updated is not None
    assert updated["graph"]["relationships"][0]["source"] == "wiki/sources/renamed"


async def test_graph_move_is_blocked_by_a_denied_typed_backlink(
    graph_writer: tuple[VaultWriter, Credential],
) -> None:
    writer, cred = graph_writer
    model = await writer.write(cred, "wiki/model.md", "---\ntype: component\n---\n# Model\n", None)
    hidden = writer.vault_dir / "private/salaries.md"
    hidden.write_text(content())
    with pytest.raises(ToolError) as exc:
        await writer.move(cred, "wiki/model.md", "wiki/new-model.md", model["version"])
    error = json.loads(str(exc.value))
    assert error["code"] == "forbidden"
    assert error["details"]["hidden_blockers"] == 1
    assert "private/salaries" not in str(error)
    assert (writer.vault_dir / "wiki/model.md").is_file()
    assert not (writer.vault_dir / "wiki/new-model.md").exists()
