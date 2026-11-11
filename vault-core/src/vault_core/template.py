"""The vault template's managed files and `.kit-manifest.json` (SPEC §9.1, plan D36).

The manifest keeps, for every released `schema_version`, a sha256 of each kit-managed file
taken with the inside of every `<!-- custom -->…<!-- /custom -->` block removed.
`scripts/upgrade-vault` uses it to tell kit content from deployer edits: a vault file whose
hash (computed the same way) matches some released version is unmodified kit content, apart
from its custom blocks, which the upgrade carries over.

Regenerate after editing managed files: `uv run python -m vault_core.template vault-template`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from vault_core import frontmatter

MANIFEST = ".kit-manifest.json"
MANAGED = (
    "AGENTS.md",
    "CLAUDE.md",
    ".gitignore",
    ".obsidian/app.json",
    ".obsidian/templates.json",
    "_system/templates/*.md",
    "skills/*/*",
    "skills/*/*/*",
)


CUSTOM_BLOCK = re.compile(r"(<!-- custom -->)(.*?)(<!-- /custom -->)", re.DOTALL)


def kit_hash(data: bytes) -> str:
    """sha256 of the file with the inside of each custom block removed (markers kept)."""
    text = data.decode("utf-8", "surrogateescape")
    return hashlib.sha256(
        CUSTOM_BLOCK.sub(r"\1\3", text).encode("utf-8", "surrogateescape")
    ).hexdigest()


def is_managed(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in MANAGED)


def schema_version(template_dir: Path) -> int:
    meta, _ = frontmatter.parse((template_dir / "AGENTS.md").read_text())
    version = (meta or {}).get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("AGENTS.md frontmatter needs an integer schema_version")
    return version


def managed_hashes(template_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(template_dir.rglob("*")):
        rel = path.relative_to(template_dir).as_posix()
        if path.is_file() and is_managed(rel):
            out[rel] = kit_hash(path.read_bytes())
    return out


def load_manifest(template_dir: Path) -> dict[str, Any]:
    path = template_dir / MANIFEST
    return json.loads(path.read_text()) if path.exists() else {"schema_version": 0, "versions": {}}


def updated_manifest(template_dir: Path) -> dict[str, Any]:
    """The manifest with the current version's entry recomputed; older versions are kept."""
    manifest = load_manifest(template_dir)
    version = schema_version(template_dir)
    manifest["schema_version"] = version
    manifest.setdefault("versions", {})[str(version)] = managed_hashes(template_dir)
    return manifest


def main(argv: list[str]) -> int:
    template_dir = Path(argv[0]) if argv else Path("vault-template")
    manifest = updated_manifest(template_dir)
    (template_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"{template_dir / MANIFEST}: schema_version {manifest['schema_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
