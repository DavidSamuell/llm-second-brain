"""scripts/setup (plan D34): non-interactive configure, idempotency, adoption, and a full
INGRESS=local deploy (§16.1 local, automated part)."""

import os
import socket
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from harness import REPO

pytestmark = pytest.mark.profile_local


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def site(tmp_path: Path) -> Path:
    env = tmp_path / "site.env"
    env.write_text(
        f"VAULT_PATH={tmp_path / 'vault'}\nDATA_PATH={tmp_path / 'data'}\nINGRESS=local\n"
        f"IGNIS_PORT={free_port()}\nMCP_PORT={free_port()}\nDOMAIN=example.test\n"
    )
    return env


def setup(env: Path, *args: str, project: str | None = None) -> subprocess.CompletedProcess[str]:
    extra = {"COMPOSE_PROJECT_NAME": project} if project else {}
    return subprocess.run(
        [str(REPO / "scripts" / "setup"), "--non-interactive", "--env-file", str(env), *args],
        cwd=REPO, env={**os.environ, **extra}, capture_output=True, text=True, check=False, timeout=1200,
    )  # fmt: skip


def values(env: Path) -> dict[str, str]:
    out = {}
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value.split("  #")[0].strip().strip("'")
    return out


def test_configure_writes_a_private_env_and_copies_the_template(site: Path) -> None:
    r = setup(site, "--no-start")
    assert r.returncode == 0, r.stderr
    env = values(site)
    assert env["COMPOSE_FILE"] == "docker-compose.yml"
    assert env["PUID"] == str(os.getuid())
    assert env["MCP_OAUTH_ISSUER"] == ""  # local: no public MCP host
    assert env["INGEST_CRON"] == "0 * * * *"
    assert stat.S_IMODE(site.stat().st_mode) == 0o600
    vault = Path(env["VAULT_PATH"])
    assert (vault / "AGENTS.md").exists()
    assert (vault / "wiki" / "sources").is_dir()
    assert not (vault / ".kit-manifest.json").exists()
    assert (Path(env["DATA_PATH"]) / "ignis-plugins").is_dir()


def test_rerunning_changes_nothing(site: Path) -> None:
    assert setup(site, "--no-start").returncode == 0
    first = site.read_text()
    home = Path(values(site)["VAULT_PATH"]) / "Home.md"
    home.write_text("# Our own home page\n")
    r = setup(site, "--no-start")
    assert r.returncode == 0, r.stderr
    assert site.read_text() == first
    assert home.read_text() == "# Our own home page\n"


def test_adopting_an_existing_vault_adds_only_missing_files(site: Path) -> None:
    vault = Path(values(site)["VAULT_PATH"])
    vault.mkdir()
    (vault / "Home.md").write_text("# Existing\n")
    (vault / "notes.md").write_text("mine\n")
    r = setup(site, "--no-start")
    assert r.returncode == 0
    assert not (vault / "AGENTS.md").exists()  # not adopted without --adopt
    assert setup(site, "--no-start", "--adopt").returncode == 0
    assert (vault / "AGENTS.md").exists()
    assert (vault / "Home.md").read_text() == "# Existing\n"
    assert (vault / "notes.md").read_text() == "mine\n"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("INGRESS=traefik", "INGRESS must be"),
        ("VAULT_PATH=relative/path", "absolute path"),
        ("INGRESS=cloudflare\nIGNIS_HOST=docs.example.com", "real hostname"),
    ],
)
def test_bad_settings_are_refused(site: Path, line: str, message: str) -> None:
    site.write_text(site.read_text() + line + "\n")
    r = setup(site, "--no-start")
    assert r.returncode != 0
    assert message in r.stderr


def test_cloudflare_and_caddy_select_their_compose_override(site: Path) -> None:
    site.write_text(
        site.read_text()
        + "INGRESS=caddy\nIGNIS_HOST=docs.corp.test\nMCP_HOST=docs-mcp.corp.test\nACME_EMAIL=a@corp.test\n"
        "OAUTH_CLIENT_ID=id\nOAUTH_CLIENT_SECRET=s\nOAUTH_ALLOWED_EMAIL_DOMAINS=corp.test\nGIT_REMOTE=git@x:y.git\n"
    )
    assert setup(site, "--no-start").returncode == 0
    env = values(site)
    assert env["COMPOSE_FILE"] == "docker-compose.yml:ingress/caddy/compose.yml"
    assert env["COMPOSE_PROFILES"] == "git"
    assert env["MCP_OAUTH_ISSUER"] == "https://docs-mcp.corp.test"
    assert len(env["OAUTH_COOKIE_SECRET"]) == 32


@pytest.fixture
def deployed(site: Path) -> Iterator[tuple[Path, str]]:
    project = f"vault-setup-e2e-{os.getpid()}"
    yield site, project
    subprocess.run(
        ["docker", "compose", "--env-file", str(site), "-p", project, "down", "-v", "--remove-orphans"],
        cwd=REPO, capture_output=True, check=False,
    )  # fmt: skip


def test_16_1_local_fresh_deploy_comes_up_healthy(deployed: tuple[Path, str]) -> None:
    site, project = deployed
    r = setup(site, project=project)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    env = values(site)
    assert httpx.get(f"http://127.0.0.1:{env['MCP_PORT']}/health", timeout=5).status_code == 200
    assert httpx.get(f"http://127.0.0.1:{env['IGNIS_PORT']}/", timeout=10).status_code == 200
