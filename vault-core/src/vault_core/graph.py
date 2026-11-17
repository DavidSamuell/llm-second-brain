"""Canonical Markdown graph extraction; no database or filesystem access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

IMPLICIT = frozenset(
    {"mentions", "sourced_from", "summarizes_raw", "belongs_to_project", "has_repo"}
)
CONFIDENCES = frozenset({"high", "medium", "low"})
STATUSES = frozenset({"current", "historical", "proposed", "disputed", "superseded"})
_ID = re.compile(r"^[a-z][a-z0-9-]*:[^\s:]+$")
_LINK = re.compile(r"!?\[\[([^\]\n]+)\]\]")
_CODE = re.compile(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$|`+[^`\n]*`+")


class NodeType(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maps_from: dict[str, str] = Field(default_factory=dict)
    explicit_only: bool = False


class Predicate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_types: list[str]
    object_types: list[str]
    requires_evidence: bool = True
    description: str = ""


class Ontology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_types: dict[str, NodeType]
    predicates: dict[str, Predicate]

    @model_validator(mode="after")
    def check_types(self) -> Ontology:
        if "concept" not in self.node_types or "source" not in self.node_types:
            raise ValueError("ontology requires concept and source node types")
        for name, rule in self.predicates.items():
            for allowed in (rule.subject_types, rule.object_types):
                if not allowed or any(t != "*" and t not in self.node_types for t in allowed):
                    raise ValueError(f"predicate {name} has unknown or empty endpoint types")
        return self


def load_ontology(text: str) -> Ontology:
    """An invalid admin ontology raises ValueError; callers retain their last valid one."""
    try:
        return Ontology.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise ValueError("invalid ontology YAML") from exc


def default_ontology() -> Ontology:
    """Built-in starter for schema-1 vaults, before their schema-2 template upgrade."""
    types = (
        "project",
        "product-spec",
        "architecture",
        "decision",
        "pipeline",
        "runbook",
        "guide",
        "component",
        "concept",
        "glossary-term",
        "team",
        "person",
        "source",
    )
    nodes = {name: NodeType(maps_from={"type": name}) for name in types}
    nodes["repo"] = NodeType(explicit_only=True)
    nodes["raw"] = NodeType(explicit_only=True)
    rules = {
        name: Predicate(subject_types=["*"], object_types=["*"], requires_evidence=False)
        for name in IMPLICIT
    }
    rules["sourced_from"].object_types = ["source"]
    rules["summarizes_raw"].subject_types = ["source"]
    rules["belongs_to_project"].object_types = ["project"]
    rules["has_repo"].subject_types = ["project"]
    rules["has_repo"].object_types = ["repo"]
    for name in ("part_of", "depends_on", "runs_on", "supersedes", "contradicts"):
        rules[name] = Predicate(subject_types=["*"], object_types=["*"])
    for name, subjects, objects in (
        ("produces", ["pipeline", "component"], ["component", "source"]),
        ("consumes", ["pipeline", "component"], ["component", "source"]),
        (
            "implements",
            ["pipeline", "component", "project"],
            ["product-spec", "architecture", "decision"],
        ),
        ("owned_by", ["*"], ["person", "team"]),
        ("decided_in", ["*"], ["decision"]),
        (
            "documented_in",
            ["*"],
            ["project", "product-spec", "architecture", "decision", "pipeline", "runbook", "guide"],
        ),
    ):
        rules[name] = Predicate(subject_types=subjects, object_types=objects)
    return Ontology(node_types=nodes, predicates=rules)


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    page: str
    title: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Edge:
    subject: str
    predicate: str
    object: str
    page: str
    source: str | None = None
    evidence: str | None = None
    confidence: str = "low"
    status: str = "current"
    explicit: bool = False
    field: str | None = None


@dataclass(frozen=True)
class GraphError:
    page: str
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class PageGraph:
    node: Node
    edges: tuple[Edge, ...]
    errors: tuple[GraphError, ...] = ()


def clean_reference(value: str) -> str:
    """Vault-relative reference without extension, display alias or heading."""
    value = value.strip().removeprefix("[[").removesuffix("]]")
    return value.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def extract(page_path: str, frontmatter: dict[str, Any], body: str, onto: Ontology) -> PageGraph:
    """Extract one page. `page:` objects are unresolved references, resolved by the catalog.

    Malformed declarations remain errors for lint rather than making browser saves fail.
    """
    errors: list[GraphError] = []
    raw = frontmatter.get("graph", {})
    if not isinstance(raw, dict):
        errors.append(GraphError(page_path, "graph", "invalid_shape", "graph must be a mapping"))
        raw = {}
    metadata = dict(frontmatter)
    metadata["type"] = {"source-summary": "source", "adr": "decision"}.get(
        str(metadata.get("type")), metadata.get("type")
    )
    node_type = raw.get("node_type")
    if not isinstance(node_type, str):
        matches = [
            (len(rule.maps_from), name)
            for name, rule in onto.node_types.items()
            if rule.maps_from and all(metadata.get(k) == v for k, v in rule.maps_from.items())
        ]
        node_type = max(matches)[1] if matches else "concept"
    node_id = raw.get("node_id", f"{node_type}:{page_path.removesuffix('.md')}")
    if not isinstance(node_id, str):
        errors.append(
            GraphError(page_path, "graph.node_id", "invalid_id", "node_id must be a string")
        )
        node_id = f"{node_type}:{page_path.removesuffix('.md')}"
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(a, str) or not a.strip() for a in aliases
    ):
        errors.append(
            GraphError(
                page_path, "graph.aliases", "invalid_shape", "aliases must be nonempty strings"
            )
        )
        aliases = []
    node = Node(
        node_id,
        node_type,
        page_path,
        str(frontmatter.get("title") or PurePosixPath(page_path).stem),
        tuple(aliases),
    )
    edges: list[Edge] = []
    relationships = raw.get("relationships", [])
    if not isinstance(relationships, list):
        errors.append(
            GraphError(
                page_path, "graph.relationships", "invalid_shape", "relationships must be a list"
            )
        )
        relationships = []
    for i, rel in enumerate(relationships):
        field = f"graph.relationships[{i}]"
        if not isinstance(rel, dict):
            errors.append(
                GraphError(page_path, field, "invalid_shape", "relationship must be a mapping")
            )
            continue
        values: dict[str, str] = {}
        for key in ("predicate", "object", "source", "evidence", "confidence", "status"):
            value = rel.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    GraphError(
                        page_path, f"{field}.{key}", "required", f"{key} must be a nonempty string"
                    )
                )
            values[key] = value.strip() if isinstance(value, str) else ""
        edges.append(
            Edge(
                node.id,
                values["predicate"],
                values["object"],
                page_path,
                values["source"],
                values["evidence"],
                values["confidence"],
                values["status"],
                True,
                field,
            )
        )
    for target in dict.fromkeys(
        clean_reference(m.group(1)) for m in _LINK.finditer(_CODE.sub("", body))
    ):
        if target:
            edges.append(Edge(node.id, "mentions", f"page:{target}", page_path))
    if node.type != "source":
        for source in _strings(frontmatter.get("sources")):
            edges.append(
                Edge(
                    node.id,
                    "sourced_from",
                    f"page:{clean_reference(source)}",
                    page_path,
                    source,
                    confidence="high",
                )
            )
    elif isinstance(frontmatter.get("raw"), str) and frontmatter["raw"].strip():
        edges.append(
            Edge(
                node.id, "summarizes_raw", f"raw:{frontmatter['raw']}", page_path, confidence="high"
            )
        )
    for project in _strings(frontmatter.get("project")):
        ref = project if ":" in project else f"page:{clean_reference(project)}"
        edges.append(Edge(node.id, "belongs_to_project", ref, page_path, confidence="high"))
    if node.type == "project":
        for repo in _strings(frontmatter.get("repo")):
            edges.append(Edge(node.id, "has_repo", f"repo:{repo}", page_path, confidence="high"))
    return PageGraph(node, tuple(dict.fromkeys(edges)), tuple(errors))


def validate(
    page_graph: PageGraph, onto: Ontology, *, source_types: dict[str, str] | None = None
) -> list[GraphError]:
    """Validate local declarations; optionally resolve source references from a catalog.

    Dangling object ids are allowed. Their existence is a catalog lint concern.
    """
    errors = list(page_graph.errors)
    node = page_graph.node
    if node.type not in onto.node_types:
        errors.append(GraphError(node.page, "graph.node_type", "unknown_type", "unknown node type"))
    if not _ID.fullmatch(node.id) or node.id.split(":", 1)[0] != node.type:
        errors.append(
            GraphError(
                node.page, "graph.node_id", "invalid_id", "node id must be <node_type>:<slug>"
            )
        )
    for i, edge in enumerate(e for e in page_graph.edges if e.explicit):
        field = edge.field or f"graph.relationships[{i}]"
        rule = onto.predicates.get(edge.predicate)
        if rule is None or edge.predicate in IMPLICIT:
            errors.append(
                GraphError(
                    node.page,
                    f"{field}.predicate",
                    "unknown_predicate",
                    "unknown or compiler-only predicate",
                )
            )
            continue
        target_type = edge.object.split(":", 1)[0]
        if not _ID.fullmatch(edge.object) or target_type not in onto.node_types:
            errors.append(
                GraphError(
                    node.page,
                    f"{field}.object",
                    "invalid_object",
                    "object must be a known type's node id",
                )
            )
        for key, value, allowed in (
            ("subject", node.type, rule.subject_types),
            ("object", target_type, rule.object_types),
        ):
            if "*" not in allowed and value not in allowed:
                errors.append(
                    GraphError(
                        node.page,
                        f"{field}.{key}",
                        "wrong_type",
                        "endpoint type is not allowed for this predicate",
                    )
                )
        if edge.confidence not in CONFIDENCES:
            errors.append(
                GraphError(
                    node.page,
                    f"{field}.confidence",
                    "invalid_confidence",
                    "expected high, medium or low",
                )
            )
        if edge.status not in STATUSES:
            errors.append(
                GraphError(
                    node.page, f"{field}.status", "invalid_status", "unknown relationship status"
                )
            )
        if rule.requires_evidence and not edge.evidence:
            errors.append(
                GraphError(
                    node.page,
                    f"{field}.evidence",
                    "missing_evidence",
                    "typed edges need an evidence quote",
                )
            )
        if source_types is not None and source_types.get(
            clean_reference(edge.source or "")
        ) not in {"source", "source-summary"}:
            errors.append(
                GraphError(
                    node.page,
                    f"{field}.source",
                    "invalid_source",
                    "source must resolve to a source-summary page",
                )
            )
    return errors
