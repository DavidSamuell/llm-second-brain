"""`scripts/upgrade-vault`: bring a vault's kit-managed files up to the kit's template
(SPEC §9.1, §16.22, plan D36).

For each managed file in the template:
- missing in the vault → added;
- same as the new template → nothing to do;
- unmodified kit content from any released version (`kit_hash` in `.kit-manifest.json`) →
  updated, carrying every `<!-- custom -->…<!-- /custom -->` block over under the same heading;
- anything else is a deployer customisation → left alone and shown as a diff;
- matched by `.vault-overrides` (gitignore syntax) → never touched.
Writes are atomic and happen under the vault lock as `upgrade-vault`; vault-watcher records
history for every changed file.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from vault_core import frontmatter
from vault_core.fsio import replace_from_temp, write_temp_sibling
from vault_core.lock import vault_lock
from vault_core.paths import through_symlink
from vault_core.template import CUSTOM_BLOCK, kit_hash, load_manifest, schema_version

OVERRIDES = ".vault-overrides"
_HEADING = re.compile(r"^#{1,6}\s+\S")

Action = Literal["add", "update", "customised", "override"]


@dataclass(frozen=True)
class Change:
    path: str
    action: Action
    diff: str = ""
    new: bytes = b""


@dataclass
class Plan:
    vault_version: int
    kit_version: int
    changes: list[Change] = field(default_factory=list)

    @property
    def to_apply(self) -> list[Change]:
        return [c for c in self.changes if c.action in ("add", "update")]


def _overrides(vault_dir: Path) -> list[str]:
    path = vault_dir / OVERRIDES
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _glob_regex(pattern: str) -> str:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return out


def overridden(rel: str, patterns: list[str]) -> bool:
    """gitignore-style: `name` matches at any depth; a pattern with a `/` is anchored to the
    vault root; `dir/` matches only folders; a matching folder covers everything inside it;
    `**` spans folders; `!pattern` re-includes. The last matching pattern wins."""
    result = False
    for raw in patterns:
        negate = raw.startswith("!")
        pattern = raw[1:] if negate else raw
        dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        anchored = "/" in pattern
        body = _glob_regex(pattern.lstrip("/"))
        prefix = "" if anchored else "(?:.*/)?"
        suffix = "/.*" if dir_only else "(?:/.*)?"
        if re.fullmatch(f"{prefix}{body}{suffix}", rel):
            result = not negate
    return result


def _custom_blocks(text: str) -> list[tuple[str | None, str]]:
    """(heading the block sits under, the block's inner text) for each custom block."""
    blocks: list[tuple[str | None, str]] = []
    for m in CUSTOM_BLOCK.finditer(text):
        before = text[: m.start()].splitlines()
        heading = next((line.strip() for line in reversed(before) if _HEADING.match(line)), None)
        blocks.append((heading, m.group(2)))
    return blocks


def carry_custom_blocks(old: str, new: str) -> str:
    """Put the vault's custom block contents into the new template text: matched by heading,
    in order; blocks whose heading is gone are appended at the end so nothing is lost."""
    pending = [b for b in _custom_blocks(old) if b[1].strip() and b[1] != _empty_inner(new, b[0])]
    if not pending:
        return new
    out, pos = [], 0
    for m in CUSTOM_BLOCK.finditer(new):
        before = new[: m.start()].splitlines()
        heading = next((line.strip() for line in reversed(before) if _HEADING.match(line)), None)
        match = next((b for b in pending if b[0] == heading), None)
        out.append(new[pos : m.start()])
        if match is not None:
            pending.remove(match)
            out.append(f"{m.group(1)}{match[1]}{m.group(3)}")
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(new[pos:])
    merged = "".join(out)
    for heading, inner in pending:
        merged += f"\n## Kept from your previous version{f' ({heading})' if heading else ''}\n\n<!-- custom -->{inner}<!-- /custom -->\n"
    return merged


def _empty_inner(new: str, heading: str | None) -> str | None:
    for h, inner in _custom_blocks(new):
        if h == heading:
            return inner
    return None


def vault_schema_version(vault_dir: Path) -> int:
    path = vault_dir / "AGENTS.md"
    if not path.is_file():
        return 0
    meta, _ = frontmatter.parse(path.read_text(errors="replace"))
    version = (meta or {}).get("schema_version")
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def plan(vault_dir: Path, template_dir: Path) -> Plan:
    manifest = load_manifest(template_dir)
    kit_version = schema_version(template_dir)
    current = manifest["versions"][str(kit_version)]
    released = {v: hashes for v, hashes in manifest["versions"].items()}
    patterns = _overrides(vault_dir)
    result = Plan(vault_schema_version(vault_dir), kit_version)
    for rel in sorted(current):
        new = (template_dir / rel).read_bytes()
        target = vault_dir / rel
        if overridden(rel, patterns):
            result.changes.append(Change(rel, "override"))
            continue
        if not target.exists():
            result.changes.append(Change(rel, "add", new=new))
            continue
        if target.is_symlink() or through_symlink(vault_dir, rel) or not target.is_file():
            result.changes.append(
                Change(rel, "customised", diff="(not a regular file; left alone)")
            )
            continue
        old = target.read_bytes()
        if old == new:
            continue
        old_text, new_text = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
        if any(hashes.get(rel) == kit_hash(old) for hashes in released.values()):
            merged = carry_custom_blocks(old_text, new_text).encode()
            if merged != old:
                result.changes.append(
                    Change(rel, "update", diff=_diff(rel, old_text, merged.decode()), new=merged)
                )
            continue
        result.changes.append(Change(rel, "customised", diff=_diff(rel, old_text, new_text)))
    return result


def _diff(rel: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"vault/{rel}", f"kit/{rel}"
        )
    )


def apply(p: Plan, vault_dir: Path, data_dir: Path) -> list[str]:
    """Write every add/update atomically under the vault lock. Returns the paths written."""
    written: list[str] = []
    with vault_lock(data_dir, "upgrade-vault"):
        for change in p.to_apply:
            target = vault_dir / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = write_temp_sibling(target, change.new, tag="upgrade")
            replace_from_temp(tmp, target)
            written.append(change.path)
    return written


def report(p: Plan) -> str:
    lines = [f"vault schema_version {p.vault_version} -> kit schema_version {p.kit_version}"]
    labels = {
        "add": "add",
        "update": "update (kit content; your custom blocks are kept)",
        "customised": "SKIP: you changed this file; merge by hand or list it in .vault-overrides",
        "override": "skip: listed in .vault-overrides",
    }
    for c in p.changes:
        lines.append(f"  {labels[c.action]}: {c.path}")
    if not p.changes:
        lines.append("  nothing to change")
    for c in p.changes:
        if c.diff and c.action in ("update", "customised"):
            lines.append("")
            lines.append(c.diff.rstrip("\n"))
    return "\n".join(lines) + "\n"
