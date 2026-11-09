"""Read-side tools: `get_schema`, `read`, `list` (SPEC §6.4; plan D8, D10, D13)."""

from __future__ import annotations

import base64
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vault_core import frontmatter
from vault_core.fsio import version_of
from vault_core.paths import PathError, is_hidden, normalize, resolve_in_vault
from vault_core.policy import Policy, check_read, under
from vault_mcp.errors import VaultToolError
from vault_mcp.store import Credential

LIST_LIMIT = 1000
_TEXT_MIME = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain"}
_REASONS = {
    "hidden": "hidden paths are never served",
    "denied": "path is in read.deny",
    "out_of_scope": "path is outside this credential's folders",
}


def _mime(path: Path) -> str:
    return (
        _TEXT_MIME.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


class ReadTools:
    def __init__(self, vault_dir: Path, policy: Policy) -> None:
        self.vault_dir = vault_dir
        self.policy = policy

    # -- helpers -----------------------------------------------------------------------------

    def _resolve(self, raw: str) -> tuple[str, Path]:
        try:
            path = normalize(raw)
            return path, resolve_in_vault(self.vault_dir, path)
        except PathError as exc:
            raise VaultToolError(exc.code, str(exc), path=raw) from None

    def _check(self, cred: Credential, path: str) -> None:
        decision = check_read(path, self.policy, cred.folders)
        if not decision.allowed:
            reason = decision.reason or "denied"
            raise VaultToolError("forbidden", _REASONS[reason], path=path, reason=reason)

    def _dir_visible(self, cred: Credential, prefix: str) -> bool:
        """A directory is listed if it is readable itself or leads to a scoped folder."""
        if is_hidden(prefix.rstrip("/")) or under(prefix, self.policy.read.deny):
            return False
        if cred.folders is None:
            return True
        return under(prefix, cred.folders) or any(f.startswith(prefix) for f in cred.folders)

    # -- tools -------------------------------------------------------------------------------

    def get_schema(self, cred: Credential) -> dict[str, Any]:
        """AGENTS.md is the vault's rulebook; every credential may read it."""
        _, agents = self._resolve("AGENTS.md")  # rejects a symlink pointing out of the vault
        try:
            content = agents.read_text()
        except FileNotFoundError:
            raise VaultToolError(
                "not_found", "the vault has no AGENTS.md", path="AGENTS.md"
            ) from None
        meta, _ = frontmatter.parse(content)
        version = meta.get("schema_version") if meta else None
        return {"schema_version": version if isinstance(version, int) else None, "content": content}

    def read(self, cred: Credential, path: str) -> dict[str, Any]:
        rel, target = self._resolve(path)
        self._check(cred, rel)
        if target.is_dir():
            raise VaultToolError("invalid", "path is a folder; use list", path=rel)
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            raise VaultToolError("not_found", "no such file", path=rel) from None
        limit = int(self.policy.write.max_file_mb * 1024 * 1024)
        if size > limit:
            raise VaultToolError(
                "too_large", "file exceeds max_file_mb", path=rel, size=size, limit=limit
            )
        data = target.read_bytes()
        out: dict[str, Any] = {"path": rel, "version": version_of(data), "mime": _mime(target)}
        try:
            out["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            out["content_base64"] = base64.b64encode(data).decode("ascii")
        return out

    def list_folder(self, cred: Credential, folder: str) -> dict[str, Any]:
        folder = folder.strip("/")
        if folder:
            rel, target = self._resolve(folder)
            prefix = rel + "/"
            if not self._dir_visible(cred, prefix):
                raise VaultToolError(
                    "forbidden", "folder is not readable with this credential", path=rel
                )
        else:
            prefix, target = "", self.vault_dir.resolve()
        if not target.exists():
            raise VaultToolError("not_found", "no such folder", path=folder)
        if not target.is_dir():
            raise VaultToolError("invalid", "path is a file; use read", path=folder)
        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            rel = prefix + child.name
            try:
                resolve_in_vault(self.vault_dir, rel)
            except PathError:
                continue  # hidden entries and symlinks that leave the vault are never listed
            if child.is_dir():
                if self._dir_visible(cred, rel + "/"):
                    entries.append({"path": rel + "/", "type": "dir"})
                continue
            if not check_read(rel, self.policy, cred.folders).allowed:
                continue
            stat = child.stat()
            entry: dict[str, Any] = {
                "path": rel,
                "type": "file",
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }
            if child.suffix.lower() == ".md":
                text = child.read_text(errors="replace")
                meta, body = frontmatter.parse(text)
                meta = meta or {}
                title = meta.get("title")
                maintainer = meta.get("maintainer")
                tags = meta.get("tags")
                entry["title"] = (
                    title if isinstance(title, str) else _first_heading(body) or child.stem
                )
                entry["maintainer"] = (
                    maintainer.strip().lower() if isinstance(maintainer, str) else None
                )
                entry["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
            entries.append(entry)
        truncated = len(entries) > LIST_LIMIT
        return {"folder": prefix, "entries": entries[:LIST_LIMIT], "truncated": truncated}
