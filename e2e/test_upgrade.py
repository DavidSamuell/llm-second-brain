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
    # A newer kit: schema_version 2 with changed wording, manifest covering versions 1 and 2.
    kit = tmp_path / "kit"
    shutil.copytree(REPO / "vault-template", kit)
    text = (kit / "AGENTS.md").read_text()
    (kit / "AGENTS.md").write_text(
        text.replace("schema_version: 1", "schema_version: 2").replace(
            "## 10. Query procedure", "## 10. Query procedure\n\nNew in v2: prefer recent pages."
        )
    )
    subprocess.run(
        ["uv", "run", "python", "-m", "vault_core.template", str(kit)],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    manifest = json.loads((kit / ".kit-manifest.json").read_text())
    assert sorted(manifest["versions"]) == ["1", "2"]

    r = subprocess.run([str(REPO / "scripts" / "upgrade-vault"), "--template", str(kit), "--yes"],
                       cwd=REPO, env=stack.env, capture_output=True, text=True, check=False, timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "update (kit content; your custom blocks are kept): AGENTS.md" in r.stdout
    upgraded = agents.read_text()
    assert "schema_version: 2" in upgraded
    assert "New in v2: prefer recent pages." in upgraded
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
