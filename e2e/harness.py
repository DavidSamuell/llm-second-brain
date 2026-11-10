"""End-to-end harness (plan D38): the real compose stack, one per ingress profile.

Each profile gets its own compose project, free host ports, and temporary VAULT_PATH and
DATA_PATH, so a run never touches a developer's stack or vault.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(r"vmcp_[A-Za-z0-9_-]{43}")

AGENTS_MD = "---\nschema_version: 1\n---\n# Vault rules\n"
SEED = {
    "AGENTS.md": AGENTS_MD,
    "wiki/index.md": "---\ntitle: Wiki index\nmaintainer: agent\n---\n# Index\n\nquarry\n",
    "projects/alpha.md": "---\nmaintainer: human\n---\n# Alpha\n\nquarry\n",
    "private/salaries.md": "not for agents, quarry\n",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def host_ip() -> str:
    """This machine's address as another machine on the network would use it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent for a UDP connect
        return str(s.getsockname()[0])


@dataclass
class Stack:
    profile: str
    project: str
    root: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.env['MCP_PORT']}"

    @property
    def ignis_url(self) -> str:
        return f"http://127.0.0.1:{self.env['IGNIS_PORT']}"

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-p", self.project, *args],
            cwd=REPO,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def token(self, *args: str) -> str:
        out = self.compose("exec", "-T", "vault-mcp", "vault-mcp", "token", *args).stdout
        match = TOKEN_RE.search(out)
        assert match, f"no token in output: {out!r}"
        return match.group(0)

    def token_cmd(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", "vault-mcp", "vault-mcp", "token", *args)


PLUGIN_FILES = {
    "main.js": b"module.exports = class GoodPlugin extends require('obsidian').Plugin {};\n",
    "manifest.json": b'{"id": "good-plugin", "name": "Good plugin", "version": "1.0.0", "minAppVersion": "1.0.0"}\n',
}


def plugin_allowlist(main_sha: str | None = None) -> str:
    import hashlib

    sums = {name: hashlib.sha256(body).hexdigest() for name, body in PLUGIN_FILES.items()}
    if main_sha:
        sums["main.js"] = main_sha
    lines = [
        "plugins:",
        "  - id: good-plugin",
        "    repo: acme/good-plugin",
        "    version: 1.0.0",
        "    sha256:",
    ]
    lines += [f'      {name}: "{digest}"' for name, digest in sums.items()]
    return "\n".join(lines) + "\n"


def start(profile: str) -> Stack:
    root = Path(tempfile.mkdtemp(prefix=f"vault-e2e-{profile}-"))
    vault, data = root / "vault", root / "data"
    for rel, text in SEED.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    data.mkdir()
    (data / "ignis-plugins").mkdir()  # bind-mount source; scripts/setup creates it too (D28)
    releases = root / "plugin-releases"
    for name, body in PLUGIN_FILES.items():
        path = releases / "acme" / "good-plugin" / "releases" / "download" / "1.0.0" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    allowlist = root / "ignis-plugins.yaml"
    allowlist.write_text(plugin_allowlist())
    (root / "alerts").mkdir()
    policy = root / "vault-mcp.yaml"
    shutil.copy(REPO / "vault-mcp.example.yaml", policy)
    env = {
        **os.environ,
        "INGRESS": profile,
        # local and none add no ingress override (D5); the e2e file adds a webhook stub.
        "COMPOSE_FILE": "docker-compose.yml:e2e/compose.e2e.yml",
        "COMPOSE_PATH_SEPARATOR": ":",
        "ALERT_WEBHOOK": "http://webhook-stub:8099/hook",
        "MONITOR_INTERVAL_S": "2",
        "E2E_ALERTS_DIR": str(root / "alerts"),
        "E2E_PLUGIN_RELEASES": str(releases),
        "IGNIS_PLUGINS_FILE": str(allowlist),
        "IGNIS_PLUGIN_BASE_URL": "http://plugin-stub:8000",
        "VAULT_PATH": str(vault),
        "DATA_PATH": str(data),
        "MCP_POLICY_FILE": str(policy),
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
        "BIND_ADDR": "127.0.0.1",
        "IGNIS_PORT": str(free_port()),
        "MCP_PORT": str(free_port()),
        # OAuth on, as in a public deployment; the issuer is a TLS stand-in proxy that
        # test_check_exposure starts on this port (nothing else needs it to be up).
        "E2E_MCP_PROXY_PORT": (proxy_port := str(free_port())),
        "MCP_OAUTH_ISSUER": f"https://127.0.0.1:{proxy_port}",
    }
    stack = Stack(profile=profile, project=f"vault-e2e-{profile}-{os.getpid()}", root=root, env=env)
    result = stack.compose("up", "-d", "--wait", "--wait-timeout", "300", check=False)
    if result.returncode != 0:
        logs = stack.compose("logs", "--no-color", check=False).stdout[-4000:]
        stop(stack)
        raise RuntimeError(f"stack failed to start:\n{result.stderr}\n{logs}")
    return stack


def stop(stack: Stack) -> None:
    stack.compose("down", "-v", "--remove-orphans", check=False)
    shutil.rmtree(stack.root, ignore_errors=True)
