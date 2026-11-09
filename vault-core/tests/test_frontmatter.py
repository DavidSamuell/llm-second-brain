import pytest

from vault_core.frontmatter import maintainer_of, parse


def test_parse_splits_yaml_frontmatter_from_body() -> None:
    meta, body = parse("---\ntitle: Search\nmaintainer: agent\n---\n# Search\n")
    assert meta == {"title": "Search", "maintainer": "agent"}
    assert body == "# Search\n"


def test_parse_without_frontmatter_returns_none_and_full_text() -> None:
    assert parse("# Title\n\ntext\n") == (None, "# Title\n\ntext\n")


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: [unclosed\n---\nbody\n",  # malformed YAML
        "---\n- a list\n---\nbody\n",  # not a mapping
        "---\ntitle: x\nno closing fence\n",  # no closing ---
    ],
)
def test_parse_treats_unusable_frontmatter_as_absent(text: str) -> None:
    meta, body = parse(text)
    assert meta is None
    assert body == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nmaintainer: human\n---\n", "human"),
        ("---\nmaintainer: Agent \n---\n", "agent"),
        ("---\ntitle: x\n---\n", None),
        ("no frontmatter", None),
        ("---\nmaintainer: [a, b]\n---\n", None),
    ],
)
def test_maintainer_of(text: str, expected: str | None) -> None:
    assert maintainer_of(text) == expected
