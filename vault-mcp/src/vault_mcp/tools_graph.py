"""Permission-filtered read tools over the watcher's derived SQLite graph."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from vault_core.graph import STATUSES, clean_reference
from vault_core.paths import PathError, normalize, resolve_in_vault
from vault_core.policy import Policy, check_read
from vault_mcp.errors import VaultToolError
from vault_mcp.store import Credential

MAX_LIMIT = 1000
MAX_PATH_DEPTH = 4


def _integer(value: int, name: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise VaultToolError("invalid", f"{name} must be between 1 and {maximum}")


def _optional_text(value: str | None, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 4096):
        raise VaultToolError("invalid", f"{name} must be a nonempty string")


def _reference(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise VaultToolError("invalid", "node reference must be a nonempty string")
    value = clean_reference(value)
    # Typed ids may contain URLs; page references must use canonical vault paths.
    path = value.removeprefix("page:")
    if ":" not in path:
        try:
            normalize(path)
        except PathError:
            raise VaultToolError(
                "invalid", "reference must be a canonical vault path or node id"
            ) from None
    return value


class _Catalog:
    """Immutable database snapshot; permissions deliberately never persist across requests."""

    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.refs: dict[str, set[str]] = defaultdict(set)
        self.aliases: dict[str, set[str]] = defaultdict(set)
        self.pages: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.outgoing: dict[str, list[int]] = defaultdict(list)
        self.incoming: dict[str, list[int]] = defaultdict(list)
        self.repos: dict[str, list[int]] = defaultdict(list)
        if db is None:
            return
        for row in db.execute("SELECT id, type, page, title FROM nodes ORDER BY page"):
            node = dict(row)
            self.nodes[node["id"]].append(node)
            self.pages[node["page"]] = node
            for ref in (
                node["id"],
                clean_reference(node["page"]),
                Path(node["page"]).stem,
                node["title"],
            ):
                self.refs[ref].add(node["page"])
        for row in db.execute("SELECT alias, id, page FROM aliases"):
            alias_node = self.pages.get(row["page"])
            if alias_node is not None and alias_node["id"] == row["id"]:
                self.refs[row["alias"]].add(row["page"])
                self.aliases[row["alias"]].add(row["page"])
        self.errors = [
            dict(row) for row in db.execute("SELECT page, field, code, message FROM errors")
        ]
        targets: dict[str, str | None] = {}
        for row in db.execute(
            "SELECT subject, predicate, object, page, source, evidence, confidence, status, explicit FROM edges ORDER BY rowid"
        ):
            edge = dict(row)
            edge["explicit"] = bool(edge["explicit"])
            reference = edge["object"]
            if reference not in targets:
                targets[reference] = (
                    self.resolve(reference)
                    if reference.startswith("page:")
                    else self.endpoint(reference)
                )
            target = targets[reference]
            edge["resolved_object"] = target
            index = len(self.edges)
            self.edges.append(edge)
            if self.endpoint(edge["subject"]) is not None and target is not None:
                self.outgoing[edge["subject"]].append(index)
                self.incoming[target].append(index)
                if target.startswith("repo:") and target not in self.nodes:
                    self.repos[target].append(index)

    def endpoint(self, id: str) -> str | None:
        if id in self.nodes:
            return id if len(self.nodes[id]) == 1 else None
        return id if id.startswith(("repo:", "raw:")) else None

    def resolve(self, reference: str) -> str | None:
        if reference in self.nodes:
            return self.endpoint(reference)
        if reference.startswith(("repo:", "raw:")):
            return reference
        ref = clean_reference(reference.removeprefix("page:"))
        # A qualified path cannot fall back to a basename or an explicit alias.
        if "/" in ref:
            node = self.pages.get(ref + ".md") or self.pages.get(ref)
            return self.endpoint(node["id"]) if node else None
        candidates = self.refs.get(ref, set())
        if len(candidates) != 1:
            return None
        return self.endpoint(self.pages[next(iter(candidates))]["id"])


class _View:
    def __init__(self, tools: GraphTools, catalog: _Catalog, cred: Credential) -> None:
        self.tools, self.catalog, self.cred = tools, catalog, cred
        self.readable: dict[str, bool] = {}
        self.visible_nodes: dict[str, dict[str, Any] | None] = {}

    def page(self, page: str) -> bool:
        if page not in self.readable:
            allowed = check_read(page, self.tools.policy, self.cred.folders).allowed
            if allowed:
                try:
                    allowed = resolve_in_vault(self.tools.vault_dir, page).is_file()
                except (PathError, OSError, ValueError):
                    allowed = False
            self.readable[page] = allowed
        return self.readable[page]

    def node(self, id: str) -> dict[str, Any] | None:
        if id in self.visible_nodes:
            return self.visible_nodes[id]
        result = None
        records = self.catalog.nodes.get(id, [])
        if len(records) == 1:
            if self.page(records[0]["page"]):
                result = records[0]
        elif not records and id.startswith("raw:"):
            page = id.removeprefix("raw:")
            if self.page(page):
                result = {"id": id, "type": "raw", "page": page, "title": Path(page).name}
        elif not records and id.startswith("repo:"):
            for index in self.catalog.repos.get(id, []):
                edge = self.catalog.edges[index]
                subjects = self.catalog.nodes.get(edge["subject"], [])
                if (
                    len(subjects) == 1
                    and self.page(edge["page"])
                    and self.page(subjects[0]["page"])
                ):
                    result = {"id": id, "type": "repo", "page": None, "title": id[5:]}
                    break
        self.visible_nodes[id] = result
        return result

    def resolve(self, ref: str) -> str | None:
        id = self.catalog.resolve(ref)
        return id if id is not None and self.node(id) is not None else None

    def edge(self, index: int) -> dict[str, Any] | None:
        edge = self.catalog.edges[index]
        target = edge["resolved_object"]
        if (
            target is None
            or not self.page(edge["page"])
            or self.node(edge["subject"]) is None
            or self.node(target) is None
        ):
            return None
        out = {key: value for key, value in edge.items() if key != "resolved_object"}
        out["object"] = target
        source = edge["source"]
        if source:
            source_id = self.catalog.resolve(source)
            source_node = self.node(source_id) if source_id else None
            if source_node is None or source_node["page"] is None:
                out["source"] = out["evidence"] = None
        elif edge["evidence"]:
            out["source"] = out["evidence"] = None
        return out


class GraphTools:
    def __init__(self, vault_dir: Path, data_dir: Path, policy: Policy) -> None:
        self.vault_dir = vault_dir
        self.db_path = (data_dir / "graph.sqlite").absolute()
        self.policy = policy
        self._signature: tuple[Any, ...] | None = None
        self._catalog = _Catalog()

    def _view(self, cred: Credential) -> _View:
        try:
            signature = tuple(
                (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if (path := Path(str(self.db_path) + suffix)).exists() and (stat := path.stat())
                else None
                for suffix in ("", "-wal")
            )
            if signature != self._signature:
                if signature[0] is None:
                    catalog = _Catalog()
                else:
                    db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
                    try:
                        db.row_factory = sqlite3.Row
                        db.execute("BEGIN")
                        tables = {
                            row[0]
                            for row in db.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if not tables:
                            catalog = _Catalog()
                        elif {"nodes", "aliases", "edges", "errors"} <= tables:
                            catalog = _Catalog(db)
                        else:
                            raise VaultToolError(
                                "invalid", "graph index schema is incomplete; rebuild the index"
                            )
                    finally:
                        db.close()
                self._catalog, self._signature = catalog, signature
        except (sqlite3.Error, OSError, ValueError):
            raise VaultToolError(
                "invalid", "graph index cannot be read; rebuild the index"
            ) from None
        return _View(self, self._catalog, cred)

    def neighbors(
        self,
        cred: Credential,
        node: str,
        predicate: str | None = None,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        _integer(depth, "depth", 2)
        _integer(limit, "limit", MAX_LIMIT)
        _optional_text(predicate, "predicate")
        if not isinstance(direction, str) or direction not in {"in", "out", "both"}:
            raise VaultToolError("invalid", "direction must be in, out or both")
        ref = _reference(node)
        view = self._view(cred)
        start = view.resolve(ref)
        if start is None:
            return {"nodes": [], "edges": []}
        visited = {start}
        frontier = [start]
        selected: dict[int, dict[str, Any]] = {}
        for _ in range(depth):
            following = []
            for current in frontier:
                indexes = []
                if direction != "in":
                    indexes.extend(view.catalog.outgoing.get(current, []))
                if direction != "out":
                    indexes.extend(view.catalog.incoming.get(current, []))
                for index in indexes:
                    if index in selected:
                        continue
                    edge = view.edge(index)
                    if edge is None or (predicate is not None and edge["predicate"] != predicate):
                        continue
                    selected[index] = edge
                    for endpoint in (edge["subject"], edge["object"]):
                        if endpoint not in visited:
                            visited.add(endpoint)
                            following.append(endpoint)
                    if len(selected) == limit:
                        return self._result(view, start, list(selected.values()))
            frontier = following
        return self._result(view, start, list(selected.values()))

    @staticmethod
    def _result(view: _View, start: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
        ids = {start}
        for edge in edges:
            ids.update((edge["subject"], edge["object"]))
        return {"nodes": [view.node(id) for id in sorted(ids)], "edges": edges}

    def edges(
        self,
        cred: Credential,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _integer(limit, "limit", MAX_LIMIT)
        _optional_text(predicate, "predicate")
        _optional_text(status, "status")
        if status is not None and status not in STATUSES:
            raise VaultToolError("invalid", "unknown graph status")
        subject_ref = _reference(subject) if subject is not None else None
        object_ref = _reference(object) if object is not None else None
        view = self._view(cred)
        subject_id = view.resolve(subject_ref) if subject_ref is not None else None
        object_id = view.resolve(object_ref) if object_ref is not None else None
        if (subject_ref is not None and subject_id is None) or (
            object_ref is not None and object_id is None
        ):
            return {"edges": []}
        indexes = (
            view.catalog.outgoing.get(subject_id, [])
            if subject_id is not None
            else view.catalog.incoming.get(object_id, [])
            if object_id is not None
            else range(len(view.catalog.edges))
        )
        result = []
        for index in indexes:
            raw = view.catalog.edges[index]
            if (
                (predicate is not None and raw["predicate"] != predicate)
                or (status is not None and raw["status"] != status)
                or (object_id is not None and raw["resolved_object"] != object_id)
            ):
                continue
            edge = view.edge(index)
            if edge is not None:
                result.append(edge)
                if len(result) == limit:
                    break
        return {"edges": result}

    def path(
        self, cred: Credential, from_node: str, to_node: str, max_depth: int = 4
    ) -> dict[str, Any]:
        _integer(max_depth, "max_depth", MAX_PATH_DEPTH)
        start_ref, target_ref = _reference(from_node), _reference(to_node)
        view = self._view(cred)
        start, target = view.resolve(start_ref), view.resolve(target_ref)
        empty: dict[str, Any] = {"paths": [], "nodes": [], "edges": []}
        if start is None or target is None:
            return empty
        queue = deque([(start, 0)])
        parents: dict[str, tuple[str, dict[str, Any]] | None] = {start: None}
        while queue:
            current, depth = queue.popleft()
            if current == target:
                ids, edges = [target], []
                parent = parents[ids[-1]]
                while parent is not None:
                    previous, path_edge = parent
                    ids.append(previous)
                    edges.append(path_edge)
                    parent = parents[ids[-1]]
                ids.reverse()
                edges.reverse()
                return {"paths": [ids], "nodes": [view.node(id) for id in ids], "edges": edges}
            if depth == max_depth:
                continue
            # Relationships are directional: a path follows subject -> object.
            for index in view.catalog.outgoing.get(current, []):
                edge = view.edge(index)
                if edge is not None and edge["object"] not in parents:
                    parents[edge["object"]] = (current, edge)
                    queue.append((edge["object"], depth + 1))
        return empty

    def backlinks(self, cred: Credential, path: str) -> dict[str, Any]:
        ref = _reference(path)
        view = self._view(cred)
        target = view.resolve(ref)
        if target is None:
            return {"pages": []}
        pages = set()
        for index in view.catalog.incoming.get(target, []):
            edge = view.edge(index)
            if edge is not None:
                pages.add(edge["page"])
        return {"pages": sorted(pages)}

    def lint(self, cred: Credential, folder: str | None = None) -> dict[str, Any]:
        prefix = None
        if folder is not None and folder != "":
            try:
                prefix = normalize(folder.rstrip("/")) + "/"
            except (PathError, AttributeError):
                raise VaultToolError("invalid", "folder must be a canonical vault path") from None
        view = self._view(cred)
        errors = []

        def report(page: str, field: str, code: str, message: str) -> None:
            if view.page(page) and (prefix is None or page.startswith(prefix)):
                errors.append({"page": page, "field": field, "code": code, "message": message})

        for error in view.catalog.errors:
            # Stored validation messages are authored by untrusted pages and can quote
            # inaccessible source paths, evidence or ids. Return only safe diagnostics.
            field = (
                error["field"] if re.fullmatch(r"[a-zA-Z0-9_.\[\]]+", error["field"]) else "graph"
            )
            code = error["code"] if re.fullmatch(r"[a-z_]+", error["code"]) else "invalid"
            report(error["page"], field, code, "Stored graph validation error.")
        for records in view.catalog.nodes.values():
            visible = [node for node in records if view.page(node["page"])]
            if len(visible) > 1:
                for node in visible:
                    report(node["page"], "graph.node_id", "duplicate_id", "Node id is not unique.")
        for pages in view.catalog.aliases.values():
            visible_pages = [page for page in pages if view.page(page)]
            if len(visible_pages) > 1:
                for page in visible_pages:
                    report(
                        page, "graph.aliases", "duplicate_alias", "Graph reference is ambiguous."
                    )
        connected: set[str] = set()
        for index, raw in enumerate(view.catalog.edges):
            edge = view.edge(index)
            if edge is not None:
                connected.update((edge["subject"], edge["object"]))
                if edge["predicate"] == "contradicts":
                    report(
                        edge["page"],
                        "graph.relationships",
                        "contradicts",
                        "Visible nodes have a contradicts relationship.",
                    )
            elif raw["resolved_object"] is None and view.page(raw["page"]):
                subject = view.catalog.endpoint(raw["subject"])
                if subject is None or view.node(subject) is None:
                    continue
                # A known but ambiguous/hidden target is not a dangling object.
                ref = raw["object"].removeprefix("page:")
                if raw["object"] in view.catalog.nodes or view.catalog.refs.get(
                    clean_reference(ref)
                ):
                    continue
                report(
                    raw["page"],
                    "graph.relationships",
                    "dangling_object",
                    "Relationship target does not exist.",
                )
        for id, records in view.catalog.nodes.items():
            if id not in connected and len(records) == 1 and view.node(id) is not None:
                report(
                    records[0]["page"], "graph", "orphan_node", "Node has no visible relationships."
                )
        unique = {(e["page"], e["field"], e["code"], e["message"]): e for e in errors}
        return {"errors": [unique[key] for key in sorted(unique)]}
