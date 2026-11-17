"""Scripted client follows the authoring protocol over the real MCP stack.

This proves the tool workflow, not a language model's obedience to the skill prose.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from harness import REPO, Stack
from test_write import _call, _writer

from vault_core import frontmatter
from vault_core.history import HistoryStore
from vault_mcp.sections import find, replace_section


@pytest.fixture
def repo_snapshot(tmp_path: Path) -> tuple[Path, str]:
    sample = tmp_path / "repo"
    sample.mkdir()

    def git(*args: str, content: str | None = None) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(sample),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                *args,
            ],
            input=content,
            text=True,
            check=True,
            capture_output=True,
        ).stdout.strip()

    git("init", "-q")
    git("remote", "add", "origin", "git@github.com:bookbot-hive/authoring-demo.git")
    readme = "# Training pipeline\nInput: curated recordings.\nSteps: validate then train.\nOutput: checkpoint.\nRuns on GPU1.\n"
    (sample / "README.md").write_text(readme)
    blob = git("hash-object", "-w", "--stdin", content=readme)
    tree = git("mktree", content=f"100644 blob {blob}\tREADME.md\n")
    revision = git("commit-tree", tree, content="Fixture pipeline\n")
    git("update-ref", "HEAD", revision)
    git("read-tree", "HEAD")
    return sample, revision


async def test_doc_authoring_document_and_ingest_finish_with_updated_human_docs(
    stack: Stack,
    repo_snapshot: tuple[Path, str],
) -> None:
    token = _writer(stack, "doc-authoring-agent")

    async def call(tool: str, **args):
        return await _call(stack, token, tool, args)

    async def create(path: str, content: str):
        return await call("write", path=path, content=content, base_version=None)

    sample, revision = repo_snapshot
    readme = sample / "README.md"
    repo = "github.com/bookbot-hive/authoring-demo"
    citation = f"{repo}@{revision}:README.md"
    raw1 = await call(
        "submit_source",
        title="Authoring pipeline repo",
        origin=f"repo:{repo}@{revision}",
        content=readme.read_text() + f"\nSource: {citation}\n",
    )
    claimed1 = await call("claim_source", path=raw1["path"])
    raw_text = await call("read", path=claimed1["path"])
    assert "GPU1" in raw_text["content"]
    name1 = Path(raw1["path"]).name
    source1 = f"wiki/sources/{name1}"
    await create(
        source1,
        "---\n"
        + yaml.safe_dump(
            {
                "title": "Training repo",
                "type": "source-summary",
                "maintainer": "agent",
                "origin": f"repo:{repo}@{revision}",
                "raw": f"raw/_processed/{name1}",
            }
        )
        + f"---\n# Training\n\nThe pipeline validates and trains on GPU1 ({citation}).\n",
    )
    source_link = source1.removesuffix(".md")
    project = "projects/authoring-alpha.md"
    pipeline = "pipelines/authoring-training.md"
    wiki = "wiki/components/authoring-training.md"
    project_meta = {
        "title": "Alpha",
        "type": "project",
        "maintainer": "shared",
        "status": "active",
        "repo": [repo],
        "sources": [source_link],
        "graph": {},
    }
    await create(
        project,
        "---\n"
        + yaml.safe_dump(project_meta)
        + f"---\n# Alpha\n\n## Summary\nTraining from curated recordings ([[{source_link}]]).\n\n## Status\nActive.\n\n## Pipelines\n[[{pipeline.removesuffix('.md')}]]\n\n## Change log\n- 2026-11-17: Created overview — [[{source_link}]]\n",
    )
    template = (
        (REPO / "vault-template/_system/templates/pipeline.md")
        .read_text()
        .replace("{{title}}", "Training")
        .replace("{{date:YYYY-MM-DD}}", "2026-11-17")
    )
    meta, body = frontmatter.parse(template)
    assert meta is not None
    meta.update(project=[project.removesuffix(".md")], sources=[source_link])
    body = replace_section(
        body,
        "Inputs and outputs",
        f"Curated recordings in; checkpoint out ({citation}; [[{source_link}]]).\n",
    )
    body = replace_section(
        body, "Steps", f"Validate then train; write a checkpoint ([[{source_link}]]).\n"
    )
    body = replace_section(body, "Where it runs", f"GPU1 ([[{source_link}]]).\n")
    body = replace_section(
        body, "Change log", f"- 2026-11-17: Created pipeline — [[{source_link}]]\n"
    )
    initial = "---\n" + yaml.safe_dump(meta) + "---\n" + body
    created = await create(pipeline, initial)
    await create(
        wiki,
        "---\n"
        + yaml.safe_dump(
            {
                "title": "Training reference",
                "type": "component",
                "maintainer": "agent",
                "project": [project.removesuffix(".md")],
                "sources": [source_link],
                "graph": {
                    "relationships": [
                        {
                            "predicate": "documented_in",
                            "object": "pipeline:pipelines/authoring-training",
                            "source": source_link,
                            "evidence": "The pipeline validates and trains on GPU1",
                            "confidence": "high",
                            "status": "current",
                        }
                    ]
                },
            }
        )
        + f"---\n# Training reference\nSee [[{pipeline.removesuffix('.md')}]] ([[{source_link}]]).\n",
    )
    await create(
        "_system/authoring-index.md",
        f"# Authoring index\n[[{project.removesuffix('.md')}]]\n[[{wiki.removesuffix('.md')}]]\n",
    )
    await create("_system/authoring-log.md", f"# Log\n- 2026-11-17 ingest [[{source_link}]]\n")
    await call("complete_source", path=claimed1["path"], summary_page=source1)
    context = await call("project_context", repo="git@github.com:bookbot-hive/authoring-demo.git")
    assert context["project"]["path"] == project
    assert len(json.dumps(context)) <= 4000
    edge_rows = (await call("graph_edges", subject=wiki, predicate="documented_in"))["edges"]
    assert any(e["object"] == "pipeline:pipelines/authoring-training" for e in edge_rows)
    # /ingest: a pasted, settled update is processed in this same run, not queued nightly.
    raw2 = await call(
        "submit_source",
        title="Authoring pipeline GPU2 decision",
        origin="conversation:team:2026-11-17",
        content="The team decided on 2026-11-17: training now runs on GPU2, not GPU1. All other steps are unchanged.",
    )
    claimed2 = await call("claim_source", path=raw2["path"])
    name2 = Path(raw2["path"]).name
    source2 = f"wiki/sources/{name2}"
    link2 = source2.removesuffix(".md")
    await create(
        source2,
        "---\n"
        + yaml.safe_dump(
            {"type": "source-summary", "maintainer": "agent", "raw": f"raw/_processed/{name2}"}
        )
        + "---\nThe team decided training now runs on GPU2; other steps are unchanged.\n",
    )
    updated = await call(
        "replace_section",
        path=pipeline,
        heading="Where it runs",
        content=f"GPU2 ([[{link2}]]).\n",
        base_version=created["version"],
        summary="Training moved to GPU2",
    )
    await call(
        "insert_after",
        path=pipeline,
        heading="Change log",
        content=f"- 2026-11-17: Training runs on GPU2 (was GPU1) — [[{link2}]]\n",
        summary="Record GPU2 decision",
    )
    await call(
        "append",
        path="_system/authoring-log.md",
        text=f"- 2026-11-17 ingest [[{link2}]] — updated pipeline location\n",
    )
    await call("complete_source", path=claimed2["path"], summary_page=source2)
    final = (await call("read", path=pipeline))["content"]
    for section in ("Inputs and outputs", "Steps", "Owners"):
        before_heading, after_heading = find(initial, section), find(final, section)
        assert (
            initial.splitlines(keepends=True)[before_heading.start : before_heading.end]
            == final.splitlines(keepends=True)[after_heading.start : after_heading.end]
        )
    assert f"GPU2 ([[{link2}]])." in final
    assert final.count("Training runs on GPU2 (was GPU1)") == 1
    history = HistoryStore(stack.root / "data").versions(pipeline)
    assert created["version"] in {version.sha256 for version in history}
    assert updated["version"] in {version.sha256 for version in history}
    for original, claimed, name in ((raw1, claimed1, name1), (raw2, claimed2, name2)):
        assert not (stack.root / "vault" / original["path"]).exists()
        assert not (stack.root / "vault" / claimed["path"]).exists()
        assert (stack.root / "vault/raw/_processed" / name).is_file()
