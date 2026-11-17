import pytest
import yaml

from vault_core.graph import default_ontology, extract, load_ontology, validate


def relationship(**changes: str) -> dict[str, str]:
    return {
        "predicate": "produces",
        "object": "component:model",
        "source": "wiki/sources/readme",
        "evidence": "The pipeline produces the model.",
        "confidence": "high",
        "status": "current",
        **changes,
    }


def test_graph_extracts_typed_and_implicit_edges_without_code_links() -> None:
    onto = default_ontology()
    page = extract(
        "pipelines/train.md",
        {
            "type": "pipeline",
            "title": "Training",
            "project": "projects/asr",
            "sources": ["wiki/sources/readme"],
            "graph": {
                "node_id": "pipeline:train",
                "aliases": ["training"],
                "relationships": [relationship()],
            },
        },
        "[[wiki/model|Model]] [[wiki/model#Details]] `[[inline]]`\n```md\n[[fenced]]\n```\n",
        onto,
    )
    assert page.node.id == "pipeline:train"
    assert page.node.aliases == ("training",)
    assert {(e.predicate, e.object) for e in page.edges} == {
        ("produces", "component:model"),
        ("mentions", "page:wiki/model"),
        ("belongs_to_project", "page:projects/asr"),
        ("sourced_from", "page:wiki/sources/readme"),
    }
    assert validate(page, onto, source_types={"wiki/sources/readme": "source-summary"}) == []


def test_links_in_inline_code_wrapped_over_lines_are_not_mentions() -> None:
    body = (
        'Run `append(log, "- ingest [[wiki/sources/<name>]] — one\nline")` then\n[[wiki/real]].\n'
    )
    body += "\nA lone ` tick, then a new paragraph\n\nwith [[wiki/after]] and ` another.\n"
    page = extract("skills/x.md", {}, body, default_ontology())
    assert {e.object for e in page.edges if e.predicate == "mentions"} == {
        "page:wiki/real",
        "page:wiki/after",
    }


def test_graph_source_and_project_frontmatter_edges() -> None:
    onto = default_ontology()
    source = extract(
        "wiki/sources/note.md", {"type": "source-summary", "raw": "raw/note.txt"}, "", onto
    )
    assert source.node.type == "source"
    assert [(e.predicate, e.object) for e in source.edges] == [
        ("summarizes_raw", "raw:raw/note.txt")
    ]
    project = extract(
        "projects/asr.md", {"type": "project", "repo": ["github.com/bookbot-hive/asr"]}, "", onto
    )
    assert [(e.predicate, e.object) for e in project.edges] == [
        ("has_repo", "repo:github.com/bookbot-hive/asr")
    ]
    assert extract("specs/a.md", {"type": "adr"}, "", onto).node.type == "decision"


def test_graph_same_filename_in_different_folders_has_distinct_ids() -> None:
    onto = default_ontology()
    assert (
        extract("wiki/a.md", {}, "", onto).node.id != extract("projects/a.md", {}, "", onto).node.id
    )


@pytest.mark.parametrize(
    ("change", "field", "code"),
    [
        ({"predicate": "invented"}, "predicate", "unknown_predicate"),
        ({"object": "person:alice"}, "object", "wrong_type"),
        ({"object": "no-node-type"}, "object", "invalid_object"),
        ({"evidence": ""}, "evidence", "missing_evidence"),
        ({"confidence": "certain"}, "confidence", "invalid_confidence"),
        ({"status": "live"}, "status", "invalid_status"),
        ({"source": "projects/not-a-source"}, "source", "invalid_source"),
    ],
)
def test_graph_validation_reports_actionable_fields(
    change: dict[str, str], field: str, code: str
) -> None:
    onto = default_ontology()
    page = extract(
        "pipelines/train.md",
        {"type": "pipeline", "graph": {"relationships": [relationship(**change)]}},
        "",
        onto,
    )
    errors = validate(page, onto, source_types={"wiki/sources/readme": "source"})
    assert any(e.code == code and e.field == f"graph.relationships[0].{field}" for e in errors)


def test_graph_wrong_subject_and_malformed_blocks_are_reported() -> None:
    onto = default_ontology()
    page = extract(
        "wiki/a.md", {"type": "person", "graph": {"relationships": [relationship()]}}, "", onto
    )
    assert any(
        e.field.endswith(".subject") and e.code == "wrong_type" for e in validate(page, onto)
    )
    for graph in (False, {"relationships": "bad"}, {"relationships": [False]}, {"aliases": [1]}):
        errors = validate(extract("wiki/a.md", {"graph": graph}, "", onto), onto)
        assert any(e.code == "invalid_shape" for e in errors)


def test_graph_ontology_rejects_unknown_endpoint_types_and_invalid_yaml() -> None:
    onto = default_ontology().model_dump()
    onto["predicates"]["produces"]["object_types"] = ["unregistered"]
    with pytest.raises(ValueError, match="unknown"):
        load_ontology(yaml.safe_dump(onto))
    with pytest.raises(ValueError):
        load_ontology("node_types: [")


def test_graph_dangling_object_is_valid_until_catalog_lint() -> None:
    onto = default_ontology()
    page = extract(
        "pipelines/new.md",
        {"type": "pipeline", "graph": {"relationships": [relationship()]}},
        "",
        onto,
    )
    assert validate(page, onto, source_types={"wiki/sources/readme": "source"}) == []


def test_graph_error_fields_keep_original_relationship_indexes() -> None:
    onto = default_ontology()
    page = extract(
        "pipelines/bad.md",
        {
            "type": "pipeline",
            "graph": {"relationships": [False, relationship(predicate="unknown")]},
        },
        "",
        onto,
    )
    errors = validate(page, onto)
    assert any(e.field == "graph.relationships[1].predicate" for e in errors)


@pytest.mark.parametrize(
    "target", ["pipeline:domains/asr/alpha/pipelines/etl", "domain:domains/asr/README"]
)
def test_documented_in_accepts_pipeline_and_domain_docs_with_source_evidence(target: str) -> None:
    onto = default_ontology()
    page = extract(
        "wiki/components/etl.md",
        {
            "type": "component",
            "graph": {"relationships": [relationship(predicate="documented_in", object=target)]},
        },
        "",
        onto,
    )
    assert validate(page, onto, source_types={"wiki/sources/readme": "source-summary"}) == []


def test_domain_overview_is_a_domain_node_and_projects_live_below_it() -> None:
    onto = default_ontology()
    domain = extract("domains/asr/README.md", {"type": "domain"}, "", onto)
    assert (domain.node.type, domain.node.id) == ("domain", "domain:domains/asr/README")
    assert validate(domain, onto) == []
    project = extract(
        "domains/asr/parakeet/README.md",
        {"type": "project", "repo": ["github.com/bookbot-hive/parakeet"]},
        "",
        onto,
    )
    assert project.node.id == "project:domains/asr/parakeet/README"
    assert project.node.id != domain.node.id
    doc = extract(
        "domains/asr/parakeet/decisions/0001-use-gpu2.md",
        {"type": "adr", "project": ["[[domains/asr/parakeet/README]]"]},
        "",
        onto,
    )
    assert doc.node.id == "decision:domains/asr/parakeet/decisions/0001-use-gpu2"
    assert ("belongs_to_project", "page:domains/asr/parakeet/README") in {
        (e.predicate, e.object) for e in doc.edges
    }
