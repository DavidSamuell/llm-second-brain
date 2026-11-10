"""SPEC §16.16: scripts/backup -> wipe -> scripts/restore reproduces vault, history, tokens,
and Ignis state."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from harness import REPO, Stack

pytestmark = pytest.mark.profile_local


def script(stack: Stack, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**stack.env, "COMPOSE_PROJECT_NAME": stack.project}
    return subprocess.run([str(REPO / "scripts" / args[0]), *args[1:]], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=600, check=False)  # fmt: skip


def wipe(folder: Path) -> None:
    for child in folder.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def test_16_16_backup_wipe_restore(stack: Stack, tmp_path: Path) -> None:
    vault, data = stack.root / "vault", stack.root / "data"
    token = stack.token(
        "create", "--name", "backup-check", "--owner", "b@example.com", "--scope", "write"
    )

    async def edit() -> None:
        async with Client(f"{stack.mcp_url}/mcp", auth=token) as c:
            await c.call_tool(
                "append", {"path": "wiki/index.md", "text": "added before the backup\n"}
            )

    asyncio.run(edit())
    # Ignis keeps its server state in DATA_PATH/ignis (plan D4); it writes there once a browser
    # session exists, so stand in for that with a file of the same kind.
    (data / "ignis" / "e2e-session.json").write_text('{"workspace": "kept"}')
    ignis_state = sorted(
        p.relative_to(data).as_posix() for p in (data / "ignis").rglob("*") if p.is_file()
    )
    history_before = stack.compose(
        "exec", "-T", "vault-watcher", "vault-watcher", "history", "list", "wiki/index.md"
    ).stdout

    out = script(stack, "backup", "--out", str(tmp_path))
    assert out.returncode == 0, out.stderr
    archive = Path(out.stdout.strip().splitlines()[-1])
    assert archive.exists()

    assert script(stack, "restore", str(archive)).returncode != 0  # refused: the stack is running
    stack.compose("stop")
    wipe(vault)
    wipe(data)
    restored = script(stack, "restore", str(archive))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    up = stack.compose("up", "-d", "--wait", "--wait-timeout", "300", check=False)
    assert up.returncode == 0, up.stderr

    assert "added before the backup" in (vault / "wiki" / "index.md").read_text()
    assert (
        sorted(p.relative_to(data).as_posix() for p in (data / "ignis").rglob("*") if p.is_file())
        >= ignis_state
    )
    history_after = stack.compose(
        "exec", "-T", "vault-watcher", "vault-watcher", "history", "list", "wiki/index.md"
    ).stdout
    assert (
        history_after.strip().splitlines()[: len(history_before.strip().splitlines())]
        == history_before.strip().splitlines()
    )

    async def read_with_old_token() -> None:
        async with Client(f"{stack.mcp_url}/mcp", auth=token) as c:
            await c.call_tool("read", {"path": "wiki/index.md"})

    asyncio.run(read_with_old_token())  # tokens.db came back
    assert archive.stat().st_mode & 0o777 == 0o600
