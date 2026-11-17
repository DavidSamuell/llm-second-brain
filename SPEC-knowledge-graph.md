# Spec: knowledge-graph

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Implemented; awaiting Checkpoint J review · **Depends on:** —

## Objective

Give agents a typed graph of how everything in the vault connects (projects, specs, pipelines, runbooks, components, concepts, people, decisions, sources), so they can answer "what depends on X", "which pipeline feeds this project" or "where is this documented" by traversal instead of full-text guessing. It follows the llm-wiki graph layer (typed relationships with evidence in page frontmatter, plus a compiled graph), adapted to a shared, permissioned server.

- **Markdown is canonical.** Relationships live in page frontmatter. The compiled graph is derived data in `DATA_PATH`, rebuilt at any time, and never committed to the vault or git.
- **Typed edges need evidence:** a source page and a quote. Plain wikilinks become low-confidence `mentions` edges for navigation only.
- **The graph respects permissions.** Callers never learn about pages they can't read.

**Users:** agents (team members' and the nightly maintenance agent), through MCP.

## Behaviour

### Page frontmatter (same shape as llm-wiki)

```yaml
graph:
  node_id: pipeline:asr-training        # optional; default <node_type>:<full-vault-relative-path-without-.md>
  aliases: [ASR training, train-asr]
  relationships:
    - predicate: produces
      object: component:asr-model
      source: wiki/sources/2026-11-17-asr-repo-readme   # a source-summary page
      evidence: "The pipeline writes the checkpoint to the model registry..."
      confidence: high                    # high | medium | low
      status: current                     # current | historical | proposed | disputed | superseded
```

### Ontology (`_system/graph/ontology.yaml`, shipped in the vault template, editable by admins only)

- **Node types:** `project`, `product-spec`, `architecture`, `decision` (ADR pages), `pipeline`, `runbook`, `guide`, `component`, `concept`, `glossary-term`, `team`, `person`, `source`, `repo`. Each maps from the page's `type`.
- **Implicit edges** made by the compiler:
  - `mentions`: from body wikilinks;
  - `sourced_from`: from `sources:`;
  - `summarizes_raw`: from a source summary's raw path;
  - `belongs_to_project`: from `project:` frontmatter;
  - `has_repo`: from a project's `repo:` list.
- **Starter typed predicates:** `part_of`, `depends_on`, `produces`, `consumes`, `runs_on`, `implements`, `owned_by`, `decided_in`, `documented_in`, `supersedes`, `contradicts`. Each has subject and object types and `requires_evidence: true`.

### Compilation (vault-watcher)

- On every indexed change (the same event path as the search index, §7.2), re-extract that page's nodes and edges into `DATA_PATH/graph.sqlite`.
- `vault-watcher reindex` rebuilds the graph along with the search index.
- Pages in `read.deny` folders and hidden paths are never compiled.
- Each node records its page path, so permission filtering can use the vault's path rules.

### Validation

- **On agent writes:** vault-mcp checks a page's `graph:` block against the ontology. An unknown predicate, wrong subject or object type, a typed edge without `source`/`evidence`, or a `source` that isn't a source-summary page → `invalid`, with `details.graph_errors` listing each problem so the agent can fix it.
- **Person edits are never refused.** `graph_lint` reports them instead.
- **Dangling objects** (a node id with no page) are allowed at write time and reported by `graph_lint`, because pages are often written in sequence.

### MCP tools (read scope)

| Tool | Returns |
|---|---|
| `graph_neighbors(node, predicate?, direction?, depth=1..2, limit=50)` | Nodes and edges around a node id or page path |
| `graph_edges(subject?, predicate?, object?, status?, limit=100)` | Matching typed edges with source, evidence, confidence and status |
| `graph_path(from_node, to_node, max_depth=4)` | One deterministic shortest directed path between two readable nodes; max_depth 1–4 |
| `backlinks(path)` | Pages that link to a page, through wikilinks or typed edges |
| `graph_lint(folder?)` | Ontology violations, missing evidence, dangling objects, orphan nodes, `contradicts` pairs |

**Permission rule:** an edge is returned only when both endpoint pages are readable by the credential (`read.deny` and `--folders`). `evidence` is returned only when its source page is readable too. Explicit-only nodes without a page (e.g. a `repo:` node) count as readable when their subject page is readable.

Declaring pages and every intermediate node must also be readable. Evidence source and quote are
redacted together when provenance is unreadable. Exact qualified paths never fall back to a
same-named page elsewhere; duplicate ids/aliases are ambiguous. Virtual raw nodes require
permission to their raw path; virtual repository nodes require a readable declaring page.

Existing template page types `source-summary` and `adr` map to graph node types `source` and
`decision`. A source is validated by its actual frontmatter `type: source|source-summary`,
not a spoofable `graph.node_type` override. Quote truth is an agent responsibility; server
validation checks required evidence and provenance type, not semantic entailment.

## Commands

```bash
uv run pytest vault-core vault-watcher vault-mcp -q -k graph
uv run pytest e2e -k graph -m "profile_local or profile_none"
docker compose exec vault-watcher vault-watcher reindex       # rebuild search + graph
```

## Project structure

- `vault-core/src/vault_core/graph.py`: frontmatter parsing, the ontology model, extraction, validation (pure functions).
- `vault-watcher/src/vault_watcher/graph_store.py`: the SQLite schema and incremental updates; hooked into the existing index pass.
- `vault-mcp/src/vault_mcp/tools_graph.py`: the five tools and permission filtering.
- `vault-template/_system/graph/ontology.yaml`, the template frontmatter, and the AGENTS.md section on typed edges.

## Code style

Pure extraction returns plain records; storage and I/O stay in the watcher.

```python
@dataclass(frozen=True)
class Edge:
    subject: str
    predicate: str
    object: str
    page: str  # page that declared it (for permission checks)
    source: str | None = None
    evidence: str | None = None
    confidence: Literal["high", "medium", "low"] = "low"
    status: str = "current"


def extract(
    page_path: str, frontmatter: dict[str, Any], body: str, onto: Ontology
) -> PageGraph: ...
def validate(page_graph: PageGraph, onto: Ontology) -> list[GraphError]: ...
```

## Testing strategy

- **Unit:**
  - extraction from fixtures: typed, mentions, sourced_from, project and repo edges;
  - validation of each error kind;
  - ontology loading;
  - permission filtering, including a `read.deny` endpoint and a folder-scoped credential.
- **Watcher:** incremental updates on edit, rename and delete; a full rebuild equals the incremental result.
- **e2e:** acceptance 1–6 below through MCP against the real stack.
- **Performance check:** 5,000 pages with 50,000 edges; `graph_neighbors` with depth 2 takes under 200 ms, and a full rebuild under 60 s on the CI runner.

## Boundaries

- **Always:** derive the graph from markdown only; filter every tool result by the caller's read permissions.
- **Ask first:** adding a graph database or another new dependency beyond SQLite; changing the frontmatter shape away from llm-wiki's.
- **Never:** let agents edit the ontology through MCP (it's on the `write.protected` list); return evidence quoted from a page the caller can't read.

## Success criteria

1. A page with a valid `produces` edge appears in `graph_edges(predicate="produces")` within 5 s of the write, and so does its `mentions` edge to a linked page.
2. A write with an unknown predicate, or without `evidence`, is refused `invalid` with `graph_errors`; the same content saved through the web is accepted and reported by `graph_lint`.
3. A credential scoped to `wiki/` gets no edges whose other end is in `projects/`, and nobody gets edges into `private/`.
4. `graph_path` from a project to a runbook returns the expected chain; `backlinks` lists linking pages.
5. Renaming a page with `move` keeps its edges, because node ids follow the new path or the explicit `node_id`.
6. Backup and restore followed by reindex reproduces the same graph (node and edge counts equal).

## Open questions

- None blocking. The ontology's node types and predicates are a starter set; admins extend it per team.
