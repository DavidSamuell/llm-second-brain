"""SPEC §16.21: with git-sync, a web edit reaches the remote; an induced conflict yields a
.conflict file and an alert; /data never appears in the repository. Plan D31 commit requests."""

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from harness import Stack

pytestmark = pytest.mark.profile_local

INTERVAL = 3


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def eventually(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.5)


@pytest.fixture
def synced(stack: Stack) -> Iterator[tuple[Path, str]]:
    bare = stack.root / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(bare))
    name = f"{stack.project}-git-sync"
    run = stack.compose(
        "run", "-d", "--no-deps", "--name", name, "-v", f"{bare}:/remote",
        "-e", "GIT_REMOTE=/remote", "-e", f"SYNC_INTERVAL={INTERVAL}", "git-sync", check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    yield bare, name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def remote_file(bare: Path, path: str) -> str | None:
    r = subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"main:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout if r.returncode == 0 else None


def test_16_21_git_sync(stack: Stack, synced: tuple[Path, str], tmp_path: Path) -> None:
    bare, container = synced
    vault = stack.root / "vault"
    eventually(
        lambda: remote_file(bare, "wiki/index.md") is not None,
        30,
        "initial commit never reached the remote",
    )

    # A web edit reaches the remote within SYNC_INTERVAL (+ margin).
    page = vault / "wiki" / "sync-test.md"
    page.write_text("# Sync test\n\nline one\n")
    eventually(
        lambda: remote_file(bare, "wiki/sync-test.md") == "# Sync test\n\nline one\n",
        INTERVAL + 60,
        "edit not pushed",
    )

    # agent-runner's commit request names the next commit (D31).
    (vault / "wiki" / "ingested.md").write_text("# Ingested\n")
    (stack.root / "data" / "git-sync" / "requests" / "1.json").write_text(
        json.dumps({"type": "ingest", "summary": "Design doc"})
    )
    eventually(
        lambda: remote_file(bare, "wiki/ingested.md") is not None,
        INTERVAL + 60,
        "request commit not pushed",
    )
    log = git("--git-dir", str(bare), "log", "--format=%s%n%b", "-n", "5", "main")
    assert "docs(ingest): Design doc" in log
    assert "Agent: agent-runner" in log

    # Induced conflict: the remote and the vault change the same line.
    subprocess.run(["docker", "pause", container], check=True, capture_output=True)
    clone = tmp_path / "clone"
    git("clone", "-q", str(bare), str(clone))
    (clone / "wiki" / "sync-test.md").write_text("# Sync test\n\nline one (remote)\n")
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "remote edit", cwd=clone)
    git("push", "-q", "origin", "main", cwd=clone)
    page.write_text("# Sync test\n\nline one (local)\n")
    subprocess.run(["docker", "unpause", container], check=True, capture_output=True)
    eventually(
        lambda: any((vault / "wiki").glob("sync-test.conflict-*.md")),
        INTERVAL + 60,
        "no .conflict file",
    )
    copy = next((vault / "wiki").glob("sync-test.conflict-*.md"))
    assert copy.read_text() == "# Sync test\n\nline one (local)\n"
    eventually(
        lambda: page.read_text() == "# Sync test\n\nline one (remote)\n",
        30,
        "remote version not applied",
    )
    alerts = stack.root / "alerts" / "alerts.jsonl"
    eventually(
        lambda: alerts.exists() and "git_sync_conflict" in alerts.read_text(),
        30,
        "no conflict alert",
    )
    eventually(
        lambda: remote_file(bare, f"wiki/{copy.name}") is not None,
        INTERVAL + 60,
        "conflict copy not pushed",
    )

    # /data is never in the repository.
    files = git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main").split()
    assert not [
        f
        for f in files
        if f.startswith("data/") or f.endswith(("tokens.db", "audit.log", "state.db"))
    ]
    assert (stack.root / "data" / "git-sync" / "heartbeat").exists()
