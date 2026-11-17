"""Permission-filtered, bounded project context for agent sessions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vault_core import frontmatter
from vault_core.graph import clean_reference
from vault_core.paths import PathError, normalize, resolve_in_vault
from vault_core.policy import Policy, check_read
from vault_core.repos import normalise_remote
from vault_mcp.errors import VaultToolError
from vault_mcp.sections import headings
from vault_mcp.store import Credential
from vault_mcp.tools_graph import GraphTools

MAX_CONTEXT = 4000
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_DATED = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?:[ :T]|$)")


def _short(text: str, maximum: int) -> str:
    if len(json.dumps(text)) <= maximum:
        return text
    low, high = 0, min(len(text), maximum)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle] + "…")) <= maximum:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


def _section(text: str, wanted: str) -> str:
    lines = text.splitlines(keepends=True)
    section = next((h for h in headings(text) if h.text.casefold() == wanted.casefold()), None)
    return "".join(lines[section.body : section.end]).strip() if section else ""


def _fits(value: dict[str, Any]) -> bool:
    # ASCII escaping is conservative for transport encoders that escape Unicode.
    return len(json.dumps(value)) <= MAX_CONTEXT


class ProjectTools:
    def __init__(
        self, vault_dir: Path, data_dir: Path, policy: Policy, graphs: GraphTools | None = None
    ) -> None:
        self.vault_dir = vault_dir
        self.policy = policy
        self.graphs = graphs or GraphTools(vault_dir, data_dir, policy)

    def _text(self, cred: Credential, rel: str) -> str | None:
        if not check_read(rel, self.policy, cred.folders).allowed:
            return None
        try:
            path = resolve_in_vault(self.vault_dir, normalize(rel))
            if (
                not path.is_file()
                or path.stat().st_size > self.policy.write.max_file_mb * 1024 * 1024
            ):
                return None
            return path.read_text()
        except (PathError, OSError, UnicodeError):
            return None

    def context(
        self, cred: Credential, repo: str | None = None, project: str | None = None
    ) -> dict[str, Any]:
        for name, value in (("repo", repo), ("project", project)):
            if value is not None and (not value.strip() or len(value) > 4096):
                raise VaultToolError("invalid", f"{name} must be a nonempty reference")
        identity = normalise_remote(repo) if repo is not None else None
        if repo is not None and identity is None:
            raise VaultToolError(
                "invalid", "repo must be a remote repository URL or canonical host/path"
            )
        if repo is None and project is None:
            return {"project": None}
        selected: list[tuple[str, dict[str, Any], str]] = []
        for path in sorted((self.vault_dir / "projects").rglob("*.md")):
            rel = path.relative_to(self.vault_dir).as_posix()
            text = self._text(cred, rel)
            if text is None:
                continue
            meta, _ = frontmatter.parse(text)
            if meta is None or meta.get("type") != "project":
                continue
            repos = meta.get("repo", [])
            repos = [repos] if isinstance(repos, str) else repos
            if identity is not None and (
                not isinstance(repos, list)
                or identity
                not in {normalise_remote(value) for value in repos if isinstance(value, str)}
            ):
                continue
            graph = meta.get("graph")
            graph = graph if isinstance(graph, dict) else {}
            refs = {
                rel,
                clean_reference(rel),
                path.stem,
                str(meta.get("title", "")),
                str(graph.get("node_id", "project:" + clean_reference(rel))),
            }
            aliases = graph.get("aliases", [])
            if isinstance(aliases, list):
                refs.update(alias for alias in aliases if isinstance(alias, str))
            if project is not None and project.strip() not in refs:
                continue
            selected.append((rel, meta, text))
        if len(selected) != 1:
            return {"project": None}
        rel, meta, text = selected[0]
        neighborhood = self.graphs.neighbors(cred, rel, depth=1, limit=50)
        project_data = {
            "path": rel,
            "title": _short(str(meta.get("title") or Path(rel).stem), 120),
            "summary": _short(_section(text, "Summary") or _section(text, "Goal"), 900),
            "status": _short(_section(text, "Status") or str(meta.get("status", "")), 180),
        }
        result: dict[str, Any] = {
            "project": project_data,
            "neighbors": {"nodes": [], "edges": []},
            "review_items": [],
            "changes": [],
        }
        if not _fits(result):
            # Preserve canonical identity rather than return a truncated, unusable path.
            project_data.update(title="", summary="", status="")
            if not _fits(result):
                raise VaultToolError("too_large", "project identity exceeds the context size limit")
        pages = {rel} | {n["page"] for n in neighborhood["nodes"] if n.get("page")}
        changes = []
        for page in sorted(pages):
            current = text if page == rel else self._text(cred, page)
            if current is None:
                continue
            for i, line in enumerate(_section(current, "Change log").splitlines()):
                if match := _DATED.match(line):
                    changes.append((match[1], i, page, line))
        for _, _, page, line in sorted(changes, reverse=True)[:5]:
            result["changes"].append(_short(f"{page}: {line}", 200))
            if not _fits(result):
                result["changes"].pop()
        queue = self._text(cred, "_system/review-queue.md")
        if queue is not None:
            wanted = {clean_reference(page) for page in pages}
            for line in queue.splitlines():
                queue_refs = [clean_reference(m.group(1)) for m in _LINK.finditer(line)]
                if (
                    not line.startswith("- [ ] ")
                    or not queue_refs
                    or not any(ref in wanted for ref in queue_refs)
                ):
                    continue
                if any(self._text(cred, ref + ".md") is None for ref in queue_refs):
                    continue
                result["review_items"].append(_short(line, 240))
                if not _fits(result):
                    result["review_items"].pop()
                if len(result["review_items"]) == 3:
                    break
        nodes = {
            n["id"]: {
                "id": n["id"],
                "type": n["type"],
                "page": n["page"],
                "title": _short(n["title"], 80),
            }
            for n in neighborhood["nodes"]
        }
        included: set[str] = set()
        for edge in neighborhood["edges"]:
            missing = {edge["subject"], edge["object"]} - included
            additions = [nodes[id] for id in sorted(missing)]
            result["neighbors"]["nodes"].extend(additions)
            result["neighbors"]["edges"].append(
                {key: edge[key] for key in ("subject", "predicate", "object", "page")}
            )
            if _fits(result):
                included.update(missing)
            else:
                result["neighbors"]["edges"].pop()
                if additions:
                    del result["neighbors"]["nodes"][-len(additions) :]
        return result
