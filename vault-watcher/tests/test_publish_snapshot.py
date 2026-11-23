"""scripts/publish-vault-snapshot against real, isolated Git repositories.

The GitHub remote is a local bare repository: a test-only Git config rewrites the canonical
https://github.com/acme/doc-vault.git to it (any other https URL goes nowhere). Only `gh`, the
GitHub API boundary, is a stub; archives come from the real create_backup and the real exporter.
"""

import gzip
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from vault_watcher.backup import create_backup

KIT = Path(__file__).resolve().parents[2]
SCRIPT = KIT / "scripts" / "publish-vault-snapshot"
REPO = "acme/doc-vault"
VAULT_ID = "3f2b8c1e-5d4a-4c6b-9e7f-0a1b2c3d4e5f"
OTHER_ID = "9a8b7c6d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
META = "vault/.backup.json"

FAKE_GH = """#!/usr/bin/env bash
# gh stub: `gh api repos/OWNER/NAME` answers with FAKE_GH_VISIBILITY; every call is logged.
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
if [ -n "${FAKE_GH_HOOK:-}" ]; then "$FAKE_GH_HOOK"; fi
[ "$#" -eq 2 ] && [ "$1" = api ] && [[ "$2" == repos/*/* ]] || { echo "unexpected gh $*" >&2; exit 1; }
v="${FAKE_GH_VISIBILITY:-private}"
jq -n --arg n "${2#repos/}" --arg v "$v" '{full_name: $n, private: ($v != "public"), visibility: $v}'
"""

# Runs inside the gh stub; on the second call (the check right before the push) another writer
# pushes to the branch first.
CONCURRENT_WRITER = """#!/usr/bin/env bash
set -euo pipefail
[ "$(wc -l < "$FAKE_GH_LOG")" -eq 2 ] || exit 0
git clone -q "$REMOTE" "$HOOK_WORK"
printf 'edited by someone else\\n' > "$HOOK_WORK/README.md"
git -C "$HOOK_WORK" commit -qam concurrent
git -C "$HOOK_WORK" push -q origin main
"""

PLUGIN: dict[str, bytes] = {
    "README.md": b"# Bookbot docs vault plugin\n",
    ".claude-plugin/marketplace.json": b'{"name": "docs-vault", "plugins": []}\n',
    "docs-vault/plugin.json": b'{"name": "docs-vault"}\n',
    "docs-vault/skills/docs-query/SKILL.md": b"---\nname: docs-query\n---\nAsk the vault.\n",
    "docs-vault/vault/not-the-snapshot.md": b"nested vault folder of the plugin\n",
    "vault.md": b"a file next to vault/, not in it\n",
    "assets/logo.bin": bytes(range(256)) * 4,
}

DOCS: dict[str, bytes] = {
    "AGENTS.md": b"---\nschema_version: 3\n---\n# Vault rules\n",
    "Home.md": b"# Vault home\n",
    "wiki/page.md": b"# Page\n\nFirst version.\n",
    "wiki/build.log": b"listed in .gitignore, still a vault file\n",
    "raw/note.md": b"# Raw note\n",
    "templates/meeting.md": b"# Meeting\r\nWindows line endings stay as they are\r\n",
    "attachments/diagram.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
    ".gitignore": b"/.obsidian/\n*.log\n",
}
PRIVATE: dict[str, bytes] = {  # vault files that must never be published
    ".obsidian/workspace.json": b'{"recent": ["wiki/page.md"]}\n',
    ".trash/deleted.md": b"# Thrown away\n",
}


@dataclass
class Site:
    root: Path
    remote: Path
    vault: Path
    data: Path
    state: Path
    env: dict[str, str]


def git(site: Site, *args: str, cwd: Path | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], env=site.env, cwd=cwd, check=True, capture_output=True
    ).stdout


def write_files(root: Path, files: dict[str, bytes]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def seed_remote(site: Site, files: dict[str, bytes]) -> str:
    work = site.root / "seed"
    git(site, "init", "-q", "-b", "main", str(work))
    write_files(work, files)
    git(site, "add", "-A", cwd=work)
    git(site, "commit", "-qm", "plugin", cwd=work)
    git(site, "push", "-q", str(site.remote), "main", cwd=work)
    return tip(site)


def tip(site: Site) -> str:
    return git(site, "--git-dir", str(site.remote), "rev-parse", "main").decode().strip()


def remote_files(site: Site, ref: str = "main") -> dict[str, bytes]:
    listing = git(site, "--git-dir", str(site.remote), "ls-tree", "-r", "-z", ref)
    files: dict[str, bytes] = {}
    for record in listing.decode().split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            files[path] = git(
                site, "--git-dir", str(site.remote), "cat-file", "blob", meta.split()[2]
            )
    return files


def outside_vault(files: dict[str, bytes]) -> dict[str, bytes]:
    return {p: b for p, b in files.items() if not p.startswith("vault/")}


def snapshot_docs(files: dict[str, bytes]) -> dict[str, bytes]:
    return {p[len("vault/") :]: b for p, b in files.items() if p.startswith("vault/") and p != META}


_last_archive_second = [""]


def make_archive(site: Site, name: str, vault_id: str = VAULT_ID) -> Path:
    """A real backup of site.vault; each one gets its own created_at second."""
    (site.data / "vault-id").write_text(vault_id)
    archive = site.root / "archives" / name
    while True:
        manifest = create_backup(site.vault, site.data, archive, [site.root / ".env"])
        if manifest["created_at"] != _last_archive_second[0]:
            break
        time.sleep(0.2)
    _last_archive_second[0] = manifest["created_at"]
    assert manifest["vault_id"] == vault_id
    return archive


def publish(
    site: Site,
    archive: Path,
    *extra: str,
    repo: str = REPO,
    vault_id: str = VAULT_ID,
    state: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(SCRIPT), "--archive", str(archive), "--repo", repo, "--vault-id", vault_id,
           "--state", str(state or site.state), *extra]  # fmt: skip
    return subprocess.run(
        cmd,
        env={**site.env, **(env or {})},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def published(result: subprocess.CompletedProcess[str], status: str) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    summary: dict[str, object] = json.loads(result.stdout)
    assert summary["status"] == status, summary
    return summary


def gh_calls(site: Site) -> list[str]:
    log = Path(site.env["FAKE_GH_LOG"])
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def site(tmp_path: Path) -> Site:
    remote = tmp_path / "remote.git"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "file://{remote}"]\n\tinsteadOf = https://github.com/{REPO}.git\n'
        f'[url "file://{tmp_path}/no-network/"]\n\tinsteadOf = https://\n'
        "[init]\n\tdefaultBranch = main\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_VISIBILITY": "private",
    }
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    s = Site(tmp_path, remote, tmp_path / "live" / "vault", tmp_path / "live" / "data",
             tmp_path / "publish-state", env)  # fmt: skip
    git(s, "init", "-q", "--bare", "-b", "main", str(remote))
    write_files(s.vault, {**DOCS, **PRIVATE})
    write_files(s.data, {"history/log.txt": b"private history\n"})
    (tmp_path / ".env").write_text("SECRET_SETTING=not-for-github\n")
    return s


def test_publish_replaces_only_vault_and_keeps_the_plugin_byte_for_byte(site: Site) -> None:
    base = seed_remote(site, PLUGIN)
    first = published(publish(site, make_archive(site, "a1.tar.gz")), "published")
    files = remote_files(site)
    assert outside_vault(files) == PLUGIN
    assert snapshot_docs(files) == DOCS  # no editor state, trash, data/ or config/
    meta = json.loads(files[META])
    assert meta == {"schema": "docs-vault-snapshot", "version": 1, "vault_id": VAULT_ID,
                    "created_at": first["created_at"], "archive_sha256": first["archive_sha256"]}  # fmt: skip
    assert git(site, "--git-dir", str(site.remote), "rev-parse", "main^").decode().strip() == base
    assert first["commit"] == tip(site)

    # Next night: one page changed, one deleted, one added.
    (site.vault / "wiki" / "page.md").write_bytes(b"# Page\n\nSecond version.\n")
    (site.vault / "raw" / "note.md").unlink()
    (site.vault / "wiki" / "new.md").write_bytes(b"# New\n")
    second = published(publish(site, make_archive(site, "a2.tar.gz")), "published")
    assert (second["added"], second["modified"], second["deleted"]) == (1, 1, 1)
    files = remote_files(site)
    assert outside_vault(files) == PLUGIN
    expected = {**DOCS, "wiki/page.md": b"# Page\n\nSecond version.\n", "wiki/new.md": b"# New\n"}
    del expected["raw/note.md"]
    assert snapshot_docs(files) == expected
    assert (
        git(site, "--git-dir", str(site.remote), "rev-parse", "main^").decode().strip()
        == first["commit"]
    )


def test_previous_snapshot_files_are_removed_and_plugin_edits_survive(site: Site) -> None:
    seed_remote(site, PLUGIN)
    published(publish(site, make_archive(site, "a1.tar.gz")), "published")
    work = site.root / "maintainer"
    git(site, "clone", "-q", str(site.remote), str(work))
    (work / "README.md").write_bytes(b"# Plugin README edited by a maintainer\n")
    git(site, "commit", "-qam", "plugin docs", cwd=work)
    git(site, "push", "-q", "origin", "main", cwd=work)

    for name in ("wiki/page.md", "wiki/build.log", "templates/meeting.md"):
        (site.vault / name).unlink()
    published(publish(site, make_archive(site, "a2.tar.gz")), "published")
    files = remote_files(site)
    assert outside_vault(files) == {
        **PLUGIN,
        "README.md": b"# Plugin README edited by a maintainer\n",
    }
    assert set(snapshot_docs(files)) == {
        "AGENTS.md",
        "Home.md",
        "raw/note.md",
        "attachments/diagram.svg",
        ".gitignore",
    }


def test_newer_snapshot_metadata_blocks_stale_publication_from_any_host(site: Site) -> None:
    seed_remote(site, PLUGIN)
    a1 = make_archive(site, "a1.tar.gz")
    a2 = make_archive(site, "a2.tar.gz")
    published(publish(site, a1), "published")
    head = tip(site)
    assert published(publish(site, a1), "unchanged")["commit"] == head

    # A fresh timestamp is published even when documents are identical, so a different
    # host cannot resurrect an intervening stale snapshot using only the remote metadata.
    newer = published(publish(site, a2), "published")
    assert newer["changed"] is True
    newest_head = tip(site)
    assert newest_head != head
    assert snapshot_docs(remote_files(site)) == DOCS
    assert json.loads(remote_files(site)[META])["archive_sha256"] == newer["archive_sha256"]
    for state in (site.state, site.root / "fresh-state"):
        result = publish(site, a1, state=state)
        assert result.returncode == 1
        assert "older" in result.stderr
    assert tip(site) == newest_head


def test_older_archive_is_refused_after_a_newer_snapshot(site: Site) -> None:
    seed_remote(site, PLUGIN)
    old = make_archive(site, "old.tar.gz")
    (site.vault / "wiki" / "page.md").write_bytes(b"# Page\n\nNewer.\n")
    new = make_archive(site, "new.tar.gz")
    published(publish(site, new), "published")
    head = tip(site)
    for state in (site.state, site.root / "fresh-state"):  # the remote metadata alone suffices
        result = publish(site, old, state=state)
        assert result.returncode == 1
        assert "older" in result.stderr
    assert tip(site) == head
    assert snapshot_docs(remote_files(site))["wiki/page.md"] == b"# Page\n\nNewer.\n"


def test_same_time_from_a_different_archive_is_refused(site: Site) -> None:
    seed_remote(site, PLUGIN)
    archive = make_archive(site, "a1.tar.gz")
    published(publish(site, archive), "published")
    head = tip(site)
    twin = site.root / "archives" / "twin.tar.gz"  # same members and manifest, other bytes
    with gzip.open(archive, "rb") as f:
        tar = f.read()
    with gzip.GzipFile(twin, "wb", compresslevel=1, mtime=0) as f:
        f.write(tar)
    assert twin.read_bytes() != archive.read_bytes()
    for state in (site.state, site.root / "fresh-state"):
        result = publish(site, twin, state=state)
        assert result.returncode == 1
        assert "different archive" in result.stderr
    assert tip(site) == head


@pytest.mark.parametrize(
    "existing",
    [
        {"vault/stale.md": b"not written by the publisher\n"},
        {"vault/stale.md": b"x\n", ".backup.json": b"{}"},
        {"vault/stale.md": b"x\n", "vault/.backup.json": b"{not json"},
        {"vault/stale.md": b"x\n", "vault/.backup.json": b'{"schema": "docs-vault-snapshot"}'},
    ],
)
def test_existing_vault_folder_without_valid_metadata_is_not_replaced(
    site: Site, existing: dict[str, bytes]
) -> None:
    head = seed_remote(site, {**PLUGIN, **existing})
    result = publish(site, make_archive(site, "a1.tar.gz"))
    assert result.returncode == 1
    assert ".backup.json" in result.stderr
    assert tip(site) == head


def test_snapshot_of_another_vault_is_not_replaced(site: Site) -> None:
    seed_remote(site, PLUGIN)
    published(publish(site, make_archive(site, "a1.tar.gz")), "published")
    head = tip(site)
    other = make_archive(site, "other.tar.gz", vault_id=OTHER_ID)
    result = publish(site, other, vault_id=OTHER_ID, state=site.root / "other-state")
    assert result.returncode == 1
    assert "another vault" in result.stderr
    assert tip(site) == head
    # An archive of the other vault cannot be passed off under this vault's id either.
    mismatch = publish(site, other)
    assert mismatch.returncode == 1
    assert tip(site) == head


def test_dry_run_reports_changes_and_pushes_nothing(site: Site) -> None:
    head = seed_remote(site, PLUGIN)
    summary = published(publish(site, make_archive(site, "a1.tar.gz"), "--dry-run"), "dry-run")
    assert summary["changed"] is True
    assert summary["added"] == len(DOCS)
    assert summary["commit"] == ""
    assert tip(site) == head
    assert remote_files(site) == PLUGIN
    assert not (site.state / "published.json").exists()


@pytest.mark.parametrize("visibility", ["public", "internal"])
def test_repository_that_is_not_private_gets_nothing(site: Site, visibility: str) -> None:
    head = seed_remote(site, PLUGIN)
    result = publish(site, make_archive(site, "a1.tar.gz"), env={"FAKE_GH_VISIBILITY": visibility})
    assert result.returncode == 1
    assert "not private" in result.stderr
    assert gh_calls(site) == [f"api repos/{REPO}"]
    assert tip(site) == head
    assert remote_files(site) == PLUGIN


def test_concurrent_remote_commit_is_never_overwritten(site: Site) -> None:
    seed_remote(site, PLUGIN)
    hook = site.root / "concurrent-writer"
    hook.write_text(CONCURRENT_WRITER)
    hook.chmod(0o755)
    archive = make_archive(site, "a1.tar.gz")
    result = publish(site, archive, env={"FAKE_GH_HOOK": str(hook), "REMOTE": str(site.remote),
                                         "HOOK_WORK": str(site.root / "other-writer")})  # fmt: skip
    assert result.returncode == 1
    assert "rejected" in result.stderr
    files = remote_files(site)
    assert files["README.md"] == b"edited by someone else\n"
    assert snapshot_docs(files) == {}
    assert not (site.state / "published.json").exists()

    # The next run builds on the concurrent commit and keeps it.
    concurrent = tip(site)
    published(publish(site, archive), "published")
    files = remote_files(site)
    assert files["README.md"] == b"edited by someone else\n"
    assert snapshot_docs(files) == DOCS
    assert (
        git(site, "--git-dir", str(site.remote), "rev-parse", "main^").decode().strip()
        == concurrent
    )


def test_state_is_bound_and_never_taken_from_a_checkout(site: Site) -> None:
    seed_remote(site, PLUGIN)
    archive = make_archive(site, "a1.tar.gz")
    published(publish(site, archive), "published")
    marker = (site.state / "publish-vault-snapshot.json").read_bytes()
    for kwargs in ({"repo": "acme/other-vault"}, {"vault_id": OTHER_ID}):
        result = publish(site, archive, **kwargs)  # type: ignore[arg-type]
        assert result.returncode == 1
        assert "belongs to another" in result.stderr
    result = publish(site, archive, "--branch", "snapshots")
    assert result.returncode == 1
    assert (site.state / "publish-vault-snapshot.json").read_bytes() == marker
    assert (site.state / "cache.git").is_dir()

    foreign = site.root / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("someone's files\n")
    result = publish(site, archive, state=foreign)
    assert result.returncode == 1
    assert sorted(p.name for p in foreign.iterdir()) == ["notes.txt"]

    checkout = site.root / "checkout"
    git(site, "init", "-q", str(checkout))
    result = publish(site, archive, state=checkout / ".publish-state")
    assert result.returncode == 1
    assert "inside a Git repository" in result.stderr
    assert not (checkout / ".publish-state").exists()
