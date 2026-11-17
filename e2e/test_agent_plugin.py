"""scripts/agent-plugin: the per-deployment plugin marketplace for Claude Code, Codex and omp."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from harness import REPO

pytestmark = pytest.mark.profile_local

URL = "https://docs-mcp.example.test/mcp"
COMMANDS = {"connect", "document", "ingest", "ask", "lint"}
SKILLS = {"docs-ingest", "docs-query", "docs-lint", "docs-capture"}


def build(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO / "scripts" / "agent-plugin"), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def frontmatter(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    assert text.startswith("---\n"), path
    head, body = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(head)
    assert isinstance(meta, dict), path
    return meta, body


def test_every_harness_gets_the_address_and_the_skills(tmp_path: Path) -> None:
    out = tmp_path / "market"
    result = build("--url", URL, "--out", str(out))
    assert result.returncode == 0, result.stderr

    plugin = out / "docs-vault"
    claude_server = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["vault"]
    portable_server = json.loads((plugin / "mcp.json").read_text())["mcpServers"]["vault"]
    assert claude_server == {"type": "http", "url": URL}
    assert portable_server == {"type": "streamable-http", "url": URL}  # omp rejects extra fields

    for catalog in (
        out / ".claude-plugin" / "marketplace.json",
        out / ".agents" / "plugins" / "marketplace.json",
    ):
        entry = json.loads(catalog.read_text())["plugins"][0]
        assert entry["name"] == "docs-vault"
    for skill in (REPO / "vault-template" / "skills").iterdir():
        assert (plugin / "skills" / skill.name / "SKILL.md").read_text() == (
            skill / "SKILL.md"
        ).read_text()

    # Rebuilding with a new address replaces the old one everywhere.
    other = "https://vault.example.test/mcp"
    assert build("--url", other, "--out", str(out)).returncode == 0
    assert URL not in "".join(p.read_text() for p in out.rglob("*.json"))


def test_plugin_ships_the_authoring_commands_and_capture_skill(tmp_path: Path) -> None:
    out = tmp_path / "market"
    assert build("--url", URL, "--out", str(out)).returncode == 0
    plugin = out / "docs-vault"

    commands = {p.stem: p for p in (plugin / "commands").glob("*.md")}
    assert set(commands) == COMMANDS
    for name, path in commands.items():
        meta, _ = frontmatter(path)
        assert meta.get("description"), name

    skills = {p.parent.name for p in (plugin / "skills").glob("*/SKILL.md")}
    assert skills == SKILLS
    for name in skills:
        meta, _ = frontmatter(plugin / "skills" / name / "SKILL.md")
        assert meta["name"] == name
        assert meta.get("description")


@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code CLI not installed")
def test_claude_validates_the_marketplace_and_plugin(tmp_path: Path) -> None:
    out = tmp_path / "market"
    assert build("--url", URL, "--out", str(out)).returncode == 0
    for target in (out, out / "docs-vault"):
        result = subprocess.run(
            ["claude", "plugin", "validate", str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        'https://docs.example.test/mcp", "x": "y',  # would break out of the JSON string
        "http://docs.example.test/mcp",  # plain http off loopback
        "javascript:alert(1)",
    ],
)
def test_unsafe_addresses_are_refused(tmp_path: Path, url: str) -> None:
    out = tmp_path / "market"
    result = build("--url", url, "--out", str(out))
    assert result.returncode != 0
    assert not out.exists()


def test_refuses_to_overwrite_an_unrelated_folder(tmp_path: Path) -> None:
    out = tmp_path / "notes"
    out.mkdir()
    (out / "keep.md").write_text("mine")
    result = build("--url", URL, "--out", str(out))
    assert result.returncode != 0
    assert (out / "keep.md").read_text() == "mine"
    assert not (out / "docs-vault").exists()
