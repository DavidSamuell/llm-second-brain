"""`ignis-init`: plugin allowlist and managed Obsidian settings, run before Ignis starts
(SPEC §8, §16.17, plan D28).

1. Every allowlisted plugin's release files are downloaded into `<data>/ignis-plugins/<id>/`
   (or kept, when already there with the right sha256). Any checksum mismatch aborts, so
   Ignis does not start with unverified code.
2. Everything else in that directory is removed: unlisted plugins, and unlisted files inside
   listed plugins. Ignis sees the directory read-only at `.obsidian/plugins`, so the UI can't
   install plugins either.
3. `.obsidian/community-plugins.json` is rewritten to the allowlist, and the kit-managed
   `.obsidian/*.json` files are reset from the template.
4. Plugins with `writable_data: true` get a `data.json` that compose bind-mounts read-write
   (`compose.plugins.yml`, from `vault-watcher harden-ignis --compose`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MANAGED_SETTINGS = ("app.json", "templates.json")
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
MAX_DOWNLOAD = 20 * 1024 * 1024
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HardenError(Exception):
    """Ignis must not start; the message tells the admin what to fix."""


@dataclass(frozen=True)
class Plugin:
    id: str
    repo: str
    version: str
    sha256: dict[str, str]
    writable_data: bool = False


def load_allowlist(path: Path) -> list[Plugin]:
    raw = yaml.safe_load(path.read_text()) if path.exists() else None
    entries = (raw or {}).get("plugins") or []
    if not isinstance(entries, list):
        raise HardenError(f"{path}: `plugins` must be a list")
    plugins: list[Plugin] = []
    for i, e in enumerate(entries):
        where = f"{path}: plugins[{i}]"
        if not isinstance(e, dict):
            raise HardenError(f"{where} must be a mapping")
        pid, repo, version, sums = (
            e.get("id"),
            e.get("repo"),
            str(e.get("version", "")),
            e.get("sha256"),
        )
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise HardenError(f"{where}: id must be lowercase letters, digits and dashes")
        if not isinstance(repo, str) or not _REPO_RE.match(repo):
            raise HardenError(f"{where}: repo must be owner/name")
        if not _VERSION_RE.match(version):
            raise HardenError(f"{where}: version is missing or invalid")
        if (
            not isinstance(sums, dict)
            or not {"main.js", "manifest.json"} <= set(sums)
            or not set(sums) <= set(PLUGIN_FILES)
        ):
            raise HardenError(
                f"{where}: sha256 needs main.js and manifest.json (and optionally styles.css)"
            )
        if not all(isinstance(v, str) and _SHA_RE.match(v) for v in sums.values()):
            raise HardenError(f"{where}: sha256 values must be 64 lowercase hex characters")
        if pid in {p.id for p in plugins}:
            raise HardenError(f"{where}: duplicate id {pid}")
        plugins.append(Plugin(pid, repo, version, dict(sums), bool(e.get("writable_data", False))))
    return plugins


def fetch(url: str) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise HardenError(f"refusing to download {url}")
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - scheme checked above
        data: bytes = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise HardenError(f"{url} is larger than {MAX_DOWNLOAD} bytes")
    return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def install(
    plugins: list[Plugin], plugins_dir: Path, base_url: str, get: Callable[[str], bytes] = fetch
) -> list[str]:
    """Bring `plugins_dir` to exactly the allowlist. Returns what was removed."""
    if plugins_dir.exists() and not os.access(plugins_dir, os.W_OK):
        raise HardenError(
            f"{plugins_dir} is not writable by this user; fix its owner (it must match PUID/PGID)"
        )
    plugins_dir.mkdir(parents=True, exist_ok=True)
    wanted = {p.id: p for p in plugins}
    removed: list[str] = []
    for entry in sorted(plugins_dir.iterdir()):
        if entry.name not in wanted:
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            removed.append(entry.name)
    for plugin in plugins:
        target = plugins_dir / plugin.id
        target.mkdir(exist_ok=True)
        keep = set(plugin.sha256) | ({"data.json"} if plugin.writable_data else set())
        for entry in sorted(target.iterdir()):
            if entry.name not in keep:
                shutil.rmtree(
                    entry
                ) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
                removed.append(f"{plugin.id}/{entry.name}")
        for name, expected in plugin.sha256.items():
            path = target / name
            if path.is_file() and not path.is_symlink() and _sha(path.read_bytes()) == expected:
                continue
            url = f"{base_url.rstrip('/')}/{plugin.repo}/releases/download/{plugin.version}/{name}"
            data = get(url)
            actual = _sha(data)
            if actual != expected:
                raise HardenError(
                    f"checksum mismatch for {plugin.id} {plugin.version} {name}: expected {expected}, got {actual}. "
                    "Ignis was not started. Check ignis/ignis-plugins.yaml."
                )
            _write_atomic(path, data)
        if plugin.writable_data:
            (target / "data.json").touch()  # the bind-mount target for its read-write data file
    return removed


def write_settings(
    vault_dir: Path, template_dir: Path, plugins: list[Plugin], plugin_data_dir: Path
) -> None:
    obsidian = vault_dir / ".obsidian"
    obsidian.mkdir(exist_ok=True)
    (obsidian / "plugins").mkdir(exist_ok=True)  # mount point: Docker would create it as root
    for name in MANAGED_SETTINGS:
        source = template_dir / ".obsidian" / name
        if source.is_file():
            _write_atomic(obsidian / name, source.read_bytes())
    _write_atomic(
        obsidian / "community-plugins.json",
        (json.dumps([p.id for p in plugins], indent=2) + "\n").encode(),
    )
    for plugin in plugins:
        if plugin.writable_data:
            data = plugin_data_dir / plugin.id / "data.json"
            data.parent.mkdir(parents=True, exist_ok=True)
            if not data.exists():
                data.write_text("{}\n")


def check_data_mounts(plugins: list[Plugin], mounted: str) -> None:
    """`compose.plugins.yml` must mount data.json for every writable plugin, or the plugin's
    settings would silently fail to save."""
    have = {m.strip() for m in mounted.split(",") if m.strip()}
    need = {p.id for p in plugins if p.writable_data}
    if need != have:
        raise HardenError(
            "the allowlist changed: regenerate compose.plugins.yml with "
            "`docker compose run --rm ignis-init harden-ignis --compose > compose.plugins.yml` "
            f"(plugins needing a data mount: {sorted(need) or 'none'}; mounted: {sorted(have) or 'none'})"
        )


def compose_override(plugins: list[Plugin]) -> str:
    writable = [p.id for p in plugins if p.writable_data]
    doc: dict[str, Any] = {
        "services": {
            "ignis-init": {"environment": {"IGNIS_PLUGIN_DATA_MOUNTS": ",".join(writable)}},
            "ignis": {
                "volumes": [
                    f"${{DATA_PATH}}/ignis-plugin-data/{pid}/data.json:/vaults/${{VAULT_NAME:-Docs}}/.obsidian/plugins/{pid}/data.json"
                    for pid in writable
                ]
            },
        }
    }
    header = "# Generated by `vault-watcher harden-ignis --compose`. Add it to COMPOSE_FILE.\n"
    return header + yaml.safe_dump(doc, sort_keys=False)


def harden(
    *, allowlist: Path, vault_dir: Path, data_dir: Path, template_dir: Path, base_url: str, mounted: str,
    get: Callable[[str], bytes] = fetch,
) -> list[str]:  # fmt: skip
    plugins = load_allowlist(allowlist)
    check_data_mounts(plugins, mounted)
    removed = install(plugins, data_dir / "ignis-plugins", base_url, get)
    write_settings(vault_dir, template_dir, plugins, data_dir / "ignis-plugin-data")
    return removed
