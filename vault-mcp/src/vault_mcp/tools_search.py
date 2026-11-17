"""`search(query, folder?, limit?)` over the watcher's FTS5 index (SPEC §6.4/§7.2, plan D23)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from vault_core.paths import PathError, is_hidden, normalize
from vault_core.policy import Policy, check_read, under
from vault_core.search_index import MAX_LIMIT, search
from vault_mcp.errors import VaultToolError
from vault_mcp.store import Credential


class SearchTools:
    def __init__(self, db_path: Path, policy: Policy) -> None:
        self.db_path = db_path
        self.policy = policy

    def _prefixes(self, cred: Credential, folder: str | None) -> list[str] | None:
        """Folders the query may match: the credential's scope, narrowed by `folder`."""
        if not folder:
            return cred.folders
        if folder.startswith("/"):
            raise VaultToolError("invalid", "folder must be relative to the vault", folder=folder)
        trimmed = folder.rstrip("/")
        try:
            prefix = normalize(trimmed) + "/"
        except PathError as exc:
            raise VaultToolError("invalid", str(exc), folder=folder) from None
        if is_hidden(trimmed) or under(prefix, self.policy.read.deny):
            raise VaultToolError("forbidden", "folder is not searchable", folder=folder)
        if cred.folders is None or under(prefix, cred.folders):
            return [prefix]
        narrowed = [f for f in cred.folders if f.startswith(prefix)]
        if not narrowed:
            raise VaultToolError(
                "forbidden", "folder is outside this credential's folders", folder=folder
            )
        return narrowed

    def search(
        self, cred: Credential, query: str, folder: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        if not 1 <= limit <= MAX_LIMIT:
            raise VaultToolError("invalid", f"limit must be between 1 and {MAX_LIMIT}", limit=limit)
        if not any(ch.isalnum() for ch in query):
            raise VaultToolError("invalid", "query has no searchable words", query=query)
        prefixes = self._prefixes(cred, folder)
        try:
            hits = search(self.db_path, query, prefixes=prefixes, limit=limit)
        except FileNotFoundError:
            raise VaultToolError(
                "not_found", "the search index is not built yet; is vault-watcher running?"
            ) from None
        # Defense in depth: the index never holds denied paths, but re-check every result.
        results = [asdict(h) for h in hits if check_read(h.path, self.policy, cred.folders).allowed]
        return {"query": query, "results": results}
