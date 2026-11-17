"""Run explicitly: uv run python vault-watcher/tests/graph_rebuild_performance.py.

Creates real canonical Markdown, then checks the spec's 5k-page/50k-edge rebuild
budget. File generation is deliberately outside the timed compilation window.
Read-side cold/warm traversal proof lives in vault-mcp/tests/test_graph.py.
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path

from vault_core.policy import Policy
from vault_watcher.graph_store import GraphStore


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vault-graph-perf-") as directory:
        root = Path(directory)
        vault, data = root / "vault", root / "data"
        (vault / "wiki").mkdir(parents=True)
        pages, degree = 5000, 10
        for i in range(pages):
            links = "\n".join(f"[[wiki/page-{(i + j) % pages}]]" for j in range(1, degree + 1))
            (vault / "wiki" / f"page-{i}.md").write_text(f"---\ntype: concept\n---\n{links}\n")
        policy = Policy.model_validate({"write": {"allow": ["wiki/"]}})
        graph = GraphStore(vault, data, policy)
        start = time.perf_counter()
        count = graph.rebuild()
        elapsed = time.perf_counter() - start
        with sqlite3.connect(graph.db_path) as db:
            nodes = db.execute("SELECT count(*) FROM nodes").fetchone()[0]
            edges = db.execute("SELECT count(*) FROM edges").fetchone()[0]
        print(
            json.dumps({"pages": count, "nodes": nodes, "edges": edges, "rebuild_seconds": elapsed})
        )
        assert count == nodes == pages
        assert edges == pages * degree
        assert elapsed < 60, f"graph rebuild exceeded 60 seconds: {elapsed:.3f}s"


if __name__ == "__main__":
    main()
