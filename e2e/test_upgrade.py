"""SPEC §16.22: upgrade-vault applies a newer template without overwriting custom blocks."""

import json
import shutil
import subprocess
import time
from pathlib import Path

from harness import REPO, Stack

ORIGINAL_HINT = "<!-- Add team-specific rules between these markers. Upgrades keep this block. -->"


def test_16_22_upgrade_keeps_custom_blocks(stack: Stack, tmp_path: Path) -> None:
    vault = stack.root / "vault"
    # Install the current template's managed files, then customise AGENTS.md's block.
    shutil.copy(REPO / "vault-template" / "AGENTS.md", vault / "AGENTS.md")
    agents = vault / "AGENTS.md"
    agents.write_text(agents.read_text().replace(ORIGINAL_HINT, "Always link the Jira ticket."))
    # A newer kit: the next schema_version, retaining every released manifest entry.
    kit = tmp_path / "kit"
    shutil.copytree(REPO / "vault-template", kit)
    current = json.loads((kit / ".kit-manifest.json").read_text())["schema_version"]
    nxt = current + 1
    text = (kit / "AGENTS.md").read_text()
    (kit / "AGENTS.md").write_text(
        text.replace(f"schema_version: {current}", f"schema_version: {nxt}").replace(
            "## 10. Query procedure",
            f"## 10. Query procedure\n\nNew in v{nxt}: prefer recent pages.",
        )
    )
    subprocess.run(
        ["uv", "run", "python", "-m", "vault_core.template", str(kit)],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    manifest = json.loads((kit / ".kit-manifest.json").read_text())
    assert sorted(manifest["versions"], key=int) == [str(v) for v in range(1, nxt + 1)]

    r = subprocess.run([str(REPO / "scripts" / "upgrade-vault"), "--template", str(kit), "--yes"],
                       cwd=REPO, env=stack.env, capture_output=True, text=True, check=False, timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    upgraded = agents.read_text()
    assert f"schema_version: {nxt}" in upgraded
    assert f"New in v{nxt}: prefer recent pages." in upgraded
    assert "<!-- custom -->\nAlways link the Jira ticket.\n<!-- /custom -->" in upgraded

    deadline = time.monotonic() + 20  # vault-watcher records the upgrade in history
    while True:
        out = stack.compose(
            "exec", "-T", "vault-watcher", "vault-watcher", "history", "list", "AGENTS.md"
        ).stdout
        if len(out.strip().splitlines()) >= 3:  # header + original + upgraded
            break
        assert time.monotonic() < deadline, out
        time.sleep(0.5)
