"""SPEC-scheduled-maintenance: scripts/maintenance with a scripted MAINTENANCE_AGENT_COMMAND that
makes real vault-mcp calls (e2e/stub/maintenance_agent.py). The script judges each outcome itself
through vault-mcp and posts one maintenance_failed alert per failed run."""

import asyncio
import fcntl
import json
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from harness import REPO, TOKEN_RE, Stack, free_port

pytestmark = pytest.mark.profile_local

STUB = REPO / "e2e" / "stub" / "maintenance_agent.py"
BROKEN = (
    "---\ntitle: Maint broken\nmaintainer: agent\n---\n# Broken\n\nSee [[wiki/missing-page]].\n"
)


@dataclass
class Hook:
    url: str
    posts: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(scope="module")
def hook() -> Iterator[Hook]:
    port = free_port()
    h = Hook(url=f"http://127.0.0.1:{port}/hook")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            h.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield h
    server.shutdown()


@dataclass
class Run:
    proc: subprocess.CompletedProcess[str]
    seen: dict[str, Any] | None
    tmpdir: Path

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr


def maintenance(stack: Stack, hook: Hook, tmp: Path, *args: str, mode: str = "ok",
                **env: str) -> Run:  # fmt: skip
    tmpdir = tmp / f"tmp-{time.monotonic_ns()}"
    tmpdir.mkdir()
    out = tmpdir.parent / f"{tmpdir.name}-seen.json"
    full_env = {
        **stack.env,
        "COMPOSE_PROJECT_NAME": stack.project,
        "ENV_FILE": "/dev/null",  # never the developer's .env
        "DATA_PATH": str(stack.root / "data"),
        "MAINTENANCE_MCP_URL": f"{stack.mcp_url}/mcp",
        "ALERT_WEBHOOK": hook.url,
        "MAINTENANCE_AGENT_COMMAND": f"{shlex.quote(sys.executable)} {shlex.quote(str(STUB))}",
        "STUB_MODE": mode,
        "STUB_OUT": str(out),
        "TMPDIR": str(tmpdir),
        **env,
    }
    proc = subprocess.run([str(REPO / "scripts" / "maintenance"), *args], cwd=REPO, env=full_env,
                          capture_output=True, text=True, timeout=300, check=False)  # fmt: skip
    seen = json.loads(out.read_text()) if out.exists() else None
    return Run(proc, seen, tmpdir)


def credentials(stack: Stack) -> list[dict[str, Any]]:
    return list(json.loads(stack.token_cmd("list", "--json").stdout))


def active(stack: Stack, name: str) -> list[dict[str, Any]]:
    return [c for c in credentials(stack) if c["name"] == name and c["status"] == "active"]


async def put(stack: Stack, token: str, path: str, content: str) -> None:
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as c:
        try:
            current = (await c.call_tool("read", {"path": path})).structured_content or {}
            base = current["version"]
        except ToolError:
            base = None
        await c.call_tool("write", {"path": path, "content": content, "base_version": base})


@dataclass
class Maint:
    stack: Stack
    hook: Hook
    seeder: str
    tokens: dict[str, str]

    def seed(self) -> None:
        async def go() -> None:
            await put(self.stack, self.seeder, "wiki/maint-broken.md", BROKEN)
            async with Client(f"{self.stack.mcp_url}/mcp", auth=self.seeder) as client:
                try:
                    index = (
                        await client.call_tool("read", {"path": "_system/index.md"})
                    ).structured_content or {}
                    content = index["content"].replace("- [[wiki/maint-broken]]\n", "")
                except ToolError:
                    content = "# Index\n\n"
                await put(self.stack, self.seeder, "_system/index.md", content)
                try:
                    await client.call_tool("read", {"path": "_system/log.md"})
                except ToolError:
                    await put(self.stack, self.seeder, "_system/log.md", "# Log\n")

        asyncio.run(go())


@pytest.fixture(scope="module")
def maint(stack: Stack, hook: Hook, tmp_path_factory: pytest.TempPathFactory) -> Maint:
    """scripts/maintenance --setup, run once for the module, next to a grant it must not touch."""
    seeder = stack.token(
        "create", "--name", "maint-seeder", "--owner", "s@example.com", "--scope", "write"
    )
    run = maintenance(
        stack, hook, tmp_path_factory.mktemp("setup"), "--setup", "--owner", "ops@example.com"
    )
    assert run.proc.returncode == 0, run.output
    assert not TOKEN_RE.search(run.output)
    state = stack.root / "data" / "maintenance"
    tokens = {f: (state / f).read_text().strip() for f in ("token", "preview-token")}
    return Maint(stack, hook, seeder, tokens)


def test_setup_creates_private_tokens_and_keeps_other_grants(maint: Maint, tmp_path: Path) -> None:
    stack, state = maint.stack, maint.stack.root / "data" / "maintenance"
    assert state.stat().st_mode & 0o777 == 0o700
    for name, scope, file in (
        ("maintenance", "write", "token"),
        ("maintenance-preview", "read", "preview-token"),
    ):
        assert (state / file).stat().st_mode & 0o777 == 0o600
        assert TOKEN_RE.fullmatch(maint.tokens[file])
        [cred] = active(stack, name)
        assert cred["scope"] == scope and cred["owner"] == "ops@example.com"
        assert cred["expires_at"] is None
    before = credentials(stack)

    again = maintenance(stack, maint.hook, tmp_path, "--setup", "--owner", "ops@example.com")
    assert again.proc.returncode == 0, again.output
    assert credentials(stack) == before  # nothing created, revoked or changed
    assert (state / "token").read_text().strip() == maint.tokens["token"]

    # A lost token file: only that credential is replaced; every other grant stays active.
    (state / "preview-token").unlink()
    redo = maintenance(stack, maint.hook, tmp_path, "--setup", "--owner", "ops@example.com")
    assert redo.proc.returncode == 0, redo.output
    assert not TOKEN_RE.search(redo.output)
    after = {c["id"]: c for c in credentials(stack)}
    for cred in before:
        if cred["name"] == "maintenance-preview":
            assert after[cred["id"]]["status"] == "revoked"
        else:
            assert after[cred["id"]]["status"] == cred["status"]
    assert len(active(stack, "maintenance-preview")) == 1
    maint.tokens["preview-token"] = (state / "preview-token").read_text().strip()
    assert (state / "preview-token").stat().st_mode & 0o777 == 0o600


def _audit(stack: Stack) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (stack.root / "data" / "audit.log").read_text().splitlines()]


def _log_text(stack: Stack) -> str:
    return "".join(
        p.read_text() for p in sorted((stack.root / "data" / "maintenance").glob("*.log"))
    )


def test_ok_run_fixes_through_mcp_with_history_and_audit(maint: Maint, tmp_path: Path) -> None:
    stack, vault = maint.stack, maint.stack.root / "vault"
    maint.seed()
    maint.hook.posts.clear()
    run = maintenance(stack, maint.hook, tmp_path)
    assert run.proc.returncode == 0, run.output
    assert maint.hook.posts == []
    assert run.seen is not None

    # The agent got its MCP config privately, outside its working folder, and no token on argv/env.
    assert run.seen["config_mode"] == 0o600 and not run.seen["config_in_cwd"]
    assert run.seen["token_on_argv"] == [] and not run.seen["token_in_env"]
    assert {"prompt.md", "skills/docs-ingest/SKILL.md", "skills/docs-lint/SKILL.md"} <= set(
        run.seen["cwd_files"]
    )
    assert not Path(run.seen["config"]).exists() and not Path(run.seen["cwd"]).exists()
    assert list(run.tmpdir.iterdir()) == []

    report = (vault / "_system" / "lint-report.md").read_text()
    run_id = report.split("maintenance_run: ", 1)[1].split("\n", 1)[0]
    assert run_id in run.seen["prompt"]
    assert "[[wiki/index]]" in (vault / "wiki" / "maint-broken.md").read_text()
    assert "- [[wiki/maint-broken]]" in (vault / "_system" / "index.md").read_text()
    assert f"maintenance {run_id}" in (vault / "_system" / "log.md").read_text()

    maint_ids = {
        r["id"]
        for r in _audit(stack)
        if r.get("credential_name") == "maintenance" and r["result"] == "ok"
    }
    assert {"write", "append"} <= {r["tool"] for r in _audit(stack) if r["id"] in maint_ids}
    pointers = stack.root / "data" / "history" / "wiki" / "maint-broken.md"
    deadline = time.monotonic() + 15
    while not any(
        json.loads(p.read_text()).get("audit_id") in maint_ids for p in pointers.glob("*.json")
    ):
        assert time.monotonic() < deadline, "no history version under maintenance"
        time.sleep(0.3)

    log = _log_text(stack)
    assert f"maintenance[{run_id}]: cost $0.42" in log
    assert f"maintenance[{run_id}]: done: ok" in log
    assert not any(t in log for t in maint.tokens.values())


def test_unresolved_findings_end_ok_without_alert(maint: Maint, tmp_path: Path) -> None:
    maint.seed()
    maint.hook.posts.clear()
    run = maintenance(maint.stack, maint.hook, tmp_path, mode="unresolved")
    assert run.proc.returncode == 0, run.output
    assert maint.hook.posts == []
    assert "done: unresolved (1 finding(s)" in run.output


@pytest.mark.parametrize(
    ("mode", "args", "reason"),
    [
        ("fail", (), "the agent exited with status 1"),
        ("fail", ("--dry-run",), "the agent exited with status 1"),
        ("noreport", (), "lint-report.md"),
        ("stale", (), "is not from this run"),
        ("errorline", (), "lists 1 error(s)"),
        ("budget", (), "stopped at the $5 budget"),
        ("garbage", (), "printed no result JSON"),
        ("malformed", (), "the agent reported an error"),
        ("errorcount", (), "lists"),
        ("sleep", ("--timeout", "3s"), "hit the 3s time limit"),
    ],
)
def test_failed_run_posts_exactly_one_alert(maint: Maint, tmp_path: Path, mode: str,
                                            args: tuple[str, ...], reason: str) -> None:  # fmt: skip
    maint.hook.posts.clear()
    run = maintenance(maint.stack, maint.hook, tmp_path, *args, mode=mode)
    assert run.proc.returncode == 1, run.output
    assert len(maint.hook.posts) == 1, maint.hook.posts
    [alert] = maint.hook.posts
    assert alert["event"] == "maintenance_failed" and alert["severity"] == "high"
    assert alert["service"] == "maintenance"
    assert reason in alert["message"]
    log_path = alert["message"].rsplit("Log: ", 1)[1]
    assert Path(log_path).is_file() and reason in Path(log_path).read_text()
    payload = json.dumps(alert)
    assert "vmcp_" not in payload and "stub failure" not in payload
    assert not any(t in Path(log_path).read_text() for t in maint.tokens.values())
    assert list(run.tmpdir.iterdir()) == []


def test_unreachable_vault_mcp_and_rejected_token_fail_before_the_agent(
    maint: Maint, tmp_path: Path
) -> None:
    maint.hook.posts.clear()
    down = maintenance(maint.stack, maint.hook, tmp_path,
                       MAINTENANCE_MCP_URL=f"http://127.0.0.1:{free_port()}/mcp")  # fmt: skip
    assert down.proc.returncode == 1
    assert down.seen is None  # the agent never started
    [alert] = maint.hook.posts
    assert alert["event"] == "maintenance_failed" and "unreachable" in alert["message"]

    maint.hook.posts.clear()
    data = tmp_path / "data"
    (data / "maintenance").mkdir(parents=True)
    fake = data / "maintenance" / "token"
    fake.write_text("vmcp_" + "A" * 43 + "\n")
    fake.chmod(0o600)
    rejected = maintenance(maint.stack, maint.hook, tmp_path, DATA_PATH=str(data))
    assert rejected.proc.returncode == 1
    assert rejected.seen is None
    [alert] = maint.hook.posts
    assert "rejected the maintenance token" in alert["message"]
    assert "vmcp_" not in json.dumps(alert)


def test_dry_run_cannot_write_and_keeps_its_preview_local(maint: Maint, tmp_path: Path) -> None:
    stack, vault = maint.stack, maint.stack.root / "vault"
    maint.seed()
    watched = [
        "wiki/maint-broken.md",
        "_system/index.md",
        "_system/log.md",
        "_system/lint-report.md",
    ]

    def snapshot() -> dict[str, bytes | None]:
        return {p: (vault / p).read_bytes() if (vault / p).exists() else None for p in watched}

    before = snapshot()
    maint.hook.posts.clear()
    run = maintenance(stack, maint.hook, tmp_path, "--dry-run", mode="dryrun")
    assert run.proc.returncode == 0, run.output
    assert run.seen is not None and run.seen["dry_run"] == "1"
    assert run.seen["write"] == "forbidden"  # refused by vault-mcp: the preview token is read-only
    assert snapshot() == before
    assert not [r for r in _audit(stack) if r.get("credential_name") == "maintenance-preview"
                and r["tool"] == "write" and r["result"] == "ok"]  # fmt: skip
    previews = list((stack.root / "data" / "maintenance").glob("*-preview-*.md"))
    assert any("Would fix 1 broken wikilink" in p.read_text() for p in previews)
    assert maint.hook.posts == []


def test_runs_never_overlap(maint: Maint, tmp_path: Path) -> None:
    maint.hook.posts.clear()
    with (maint.stack.root / "data" / "maintenance" / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = maintenance(maint.stack, maint.hook, tmp_path)
    assert run.proc.returncode == 0
    assert "still going; skipped" in run.output
    assert run.seen is None and maint.hook.posts == []
