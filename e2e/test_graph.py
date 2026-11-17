"""Knowledge-graph success criteria against both real compose profiles."""

import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastmcp.exceptions import ToolError
from harness import REPO, Stack
from test_backup_restore import script
from test_write import _call, _writer

from vault_watcher.backup import restore

SOURCE = "wiki/sources/graph-evidence"


def document(
    kind: str, id: str, relationships: list[tuple[str, str]] | None = None, body: str = ""
) -> str:
    meta = {
        "type": kind,
        "graph": {
            "node_id": id,
            "relationships": [
                {
                    "predicate": predicate,
                    "object": target,
                    "source": SOURCE,
                    "evidence": "The project uses the training pipeline and model serving runbook.",
                    "confidence": "high",
                    "status": "current",
                }
                for predicate, target in (relationships or [])
            ],
        },
    }
    return "---\n" + yaml.safe_dump(meta) + "---\n" + body


async def test_graph_write_query_permissions_move_and_web_lint(stack: Stack) -> None:
    token = _writer(stack, "graph-e2e")

    async def write(path: str, text: str) -> dict[str, str]:
        return await _call(
            stack, token, "write", {"path": path, "content": text, "base_version": None}
        )

    await write(
        SOURCE + ".md",
        "---\ntype: source-summary\n---\nThe project uses the training pipeline and model serving runbook.\n",
    )
    model = await write("wiki/graph-model.md", "---\ntype: component\n---\n# Model\n")
    await write("runbooks/graph-serve.md", document("runbook", "runbook:graph-serve"))
    await write(
        "projects/graph-project.md",
        document("project", "project:graph-project", [("depends_on", "pipeline:graph-train")]),
    )
    started = time.monotonic()
    await write(
        "pipelines/graph-train.md",
        document(
            "pipeline",
            "pipeline:graph-train",
            [
                ("produces", "component:wiki/graph-model"),
                ("documented_in", "runbook:graph-serve"),
            ],
            "[[wiki/graph-model]]\n",
        ),
    )
    while True:
        edges = (await _call(stack, token, "graph_edges", {"subject": "pipeline:graph-train"}))[
            "edges"
        ]
        if {e["predicate"] for e in edges} >= {"produces", "mentions"}:
            break
        assert time.monotonic() - started < 5
        await asyncio.sleep(0.05)
    assert time.monotonic() - started < 5
    path = await _call(
        stack,
        token,
        "graph_path",
        {"from_node": "project:graph-project", "to_node": "runbook:graph-serve"},
    )
    assert path["paths"] == [
        ["project:graph-project", "pipeline:graph-train", "runbook:graph-serve"]
    ]
    neighbors = await _call(
        stack, token, "graph_neighbors", {"node": "projects/graph-project.md", "depth": 2}
    )
    assert "pipeline:graph-train" in {n["id"] for n in neighbors["nodes"]}
    backlinks = await _call(stack, token, "backlinks", {"path": "wiki/graph-model.md"})
    assert "pipelines/graph-train.md" in backlinks["pages"]
    await write(
        "wiki/graph-reference.md",
        document(
            "component", "component:graph-reference", [("depends_on", "project:graph-project")]
        ),
    )
    private = stack.root / "vault/private/graph-secret.md"
    private.write_text(document("component", "component:graph-secret"))
    await write(
        "wiki/graph-private-ref.md",
        document(
            "component", "component:graph-private-ref", [("depends_on", "component:graph-secret")]
        ),
    )
    scoped = stack.token(
        "create",
        "--name",
        "graph-scoped",
        "--owner",
        "g@example.com",
        "--scope",
        "read",
        "--folders",
        "wiki/",
    )
    scoped_result = await _call(stack, scoped, "graph_edges", {})
    assert "project:graph-project" not in json.dumps(scoped_result)
    assert "component:graph-secret" not in json.dumps(await _call(stack, token, "graph_edges", {}))
    assert not (
        await _call(
            stack,
            scoped,
            "graph_path",
            {"from_node": "component:graph-reference", "to_node": "runbook:graph-serve"},
        )
    )["paths"]
    await _call(
        stack,
        token,
        "move",
        {
            "path": "wiki/graph-model.md",
            "new_path": "wiki/graph-renamed-model.md",
            "base_version": model["version"],
        },
    )
    moved = (await _call(stack, token, "graph_edges", {"predicate": "produces"}))["edges"]
    assert any(e["object"] == "component:wiki/graph-renamed-model" for e in moved)
    assert (
        "pipelines/graph-train.md"
        in (await _call(stack, token, "backlinks", {"path": "wiki/graph-renamed-model.md"}))[
            "pages"
        ]
    )
    for missing_evidence in (False, True):
        bad = document(
            "pipeline",
            "pipeline:invalid",
            [
                (
                    "unknown" if not missing_evidence else "produces",
                    "component:wiki/graph-renamed-model",
                )
            ],
        )
        if missing_evidence:
            meta = yaml.safe_load(bad.split("---", 2)[1])
            del meta["graph"]["relationships"][0]["evidence"]
            bad = "---\n" + yaml.safe_dump(meta) + "---\n"
        with pytest.raises(ToolError) as exc:
            await write("wiki/graph-invalid.md", bad)
        error = json.loads(str(exc.value))
        assert error["code"] == "invalid" and error["details"]["graph_errors"]
        assert not (stack.root / "vault/wiki/graph-invalid.md").exists()
    web = stack.root / "vault/wiki/graph-web-invalid.md"
    web.write_text(bad)
    # Browser saves use the existing 5 s quiet debounce; only MCP writes have a <5 s SLA.
    deadline = time.monotonic() + 10
    while True:
        errors = (await _call(stack, token, "graph_lint", {"folder": "wiki"}))["errors"]
        if any(
            e["page"] == "wiki/graph-web-invalid.md" and e["field"].endswith("evidence")
            for e in errors
        ):
            break
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
    assert web.read_text() == bad


def counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT count(*) FROM nodes").fetchone()[0],
            db.execute("SELECT count(*) FROM edges").fetchone()[0],
        )


def test_graph_backup_restore_reindex_reproduces_catalog(stack: Stack, tmp_path: Path) -> None:
    # This scenario also works alone: canonical Markdown is authored before reindex.
    page = stack.root / "vault/wiki/graph-backup.md"
    page.write_text("---\ntype: concept\n---\n[[wiki/index]]\n")
    stack.compose("exec", "-T", "vault-watcher", "vault-watcher", "reindex")
    before = counts(stack.root / "data/graph.sqlite")
    backup = script(stack, "backup", "--out", str(tmp_path / "archives"))
    assert backup.returncode == 0, backup.stderr
    archive = Path(backup.stdout.strip().splitlines()[-1])
    target = tmp_path / "restored"
    (target / "vault").mkdir(parents=True)
    (target / "data").mkdir()
    restore(archive, target / "vault", target / "data", target / "config")
    # Delete derived state to prove canonical Markdown, not the backup index, reproduces it.
    (target / "data/graph.sqlite").unlink()
    import os

    result = subprocess.run(
        ["uv", "run", "vault-watcher", "reindex"],
        cwd=REPO,
        env={
            **os.environ,
            "VAULT_DIR": str(target / "vault"),
            "DATA_DIR": str(target / "data"),
            "POLICY_FILE": str(stack.root / "vault-mcp.yaml"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert counts(target / "data/graph.sqlite") == before
