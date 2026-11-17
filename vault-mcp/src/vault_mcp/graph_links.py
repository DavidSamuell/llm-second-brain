"""Surgical YAML reference updates for moves; evidence quotes and explicit ids stay intact."""

from __future__ import annotations

import json
from collections.abc import Iterator

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from vault_core.graph import clean_reference
from vault_mcp.errors import VaultToolError


def _field(node: Node | None, name: str) -> Node | None:
    if isinstance(node, MappingNode):
        return next((v for k, v in node.value if k.value == name), None)
    return None


def _items(node: Node | None) -> list[Node]:
    if isinstance(node, SequenceNode):
        return list(node.value)
    return [node] if node is not None else []


def _references(text: str) -> Iterator[ScalarNode]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if end is None:
        return
    # Including a blank first line keeps YAML marks aligned with the original text.
    try:
        root = yaml.compose("\n" + "".join(lines[1:end]))
    except yaml.YAMLError:
        return
    for name in ("sources", "project"):
        for item in _items(_field(root, name)):
            if isinstance(item, ScalarNode):
                yield item
    graph = _field(root, "graph")
    for relationship in _items(_field(graph, "relationships")):
        for name in ("object", "source"):
            reference_node = _field(relationship, name)
            if isinstance(reference_node, ScalarNode):
                yield reference_node


def rewrite_graph_links(
    text: str,
    old_path: str,
    new_path: str,
    old_id: str,
    new_id: str,
    known_paths: frozenset[str] = frozenset(),
) -> str:
    """Rewrite paths, unambiguous bare references and derived ids; preserve evidence."""
    replacements: list[tuple[int, int, str]] = []
    for node in _references(text):
        value = node.value
        ref = clean_reference(value)
        new: str | None = None
        if value == old_id and old_id != new_id:
            new = new_id
        elif ref == old_path.removesuffix(".md") or (
            "/" not in ref
            and ref == old_path.rsplit("/", 1)[-1].removesuffix(".md")
            and sum(p.rsplit("/", 1)[-1].removesuffix(".md") == ref for p in known_paths) == 1
        ):
            new = new_path if value.endswith(".md") else new_path.removesuffix(".md")
        if new is not None and new != value:
            # Marks are relative to the synthetic one-character first line, not '---\n'.
            offset = len(text.splitlines(keepends=True)[0]) - 1
            if (
                text[node.start_mark.index + offset : node.end_mark.index + offset]
                .lstrip()
                .startswith("&")
            ):
                raise VaultToolError(
                    "invalid",
                    "expand YAML anchors used by graph references before moving this page",
                )
            replacements.append(
                (node.start_mark.index + offset, node.end_mark.index + offset, json.dumps(new))
            )
    for start, end, value in sorted(set(replacements), reverse=True):
        text = text[:start] + value + text[end:]
    return text
