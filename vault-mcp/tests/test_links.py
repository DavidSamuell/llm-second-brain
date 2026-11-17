import pytest

from vault_mcp.links import VaultFiles, links_to, rewrite_links

FILES = VaultFiles(
    [
        "wiki/concepts/search.md",
        "wiki/index.md",
        "projects/alpha.md",
        "projects/notes/index.md",  # a second "index"
        "attachments/diagram.png",
    ]
)


@pytest.mark.parametrize(
    ("text", "source", "expected"),
    [
        ("see [[search]]", "wiki/index.md", True),  # unique basename
        ("see [[wiki/concepts/search]]", "projects/alpha.md", True),  # full path
        ("see [[search.md|Search]]", "wiki/index.md", True),  # extension + alias
        ("see [[search#Ranking]]", "wiki/index.md", True),  # heading
        ("![[search]]", "wiki/index.md", True),  # embed
        ("[text](concepts/search.md)", "wiki/index.md", True),  # relative markdown link
        ("[text](../wiki/concepts/search.md#x)", "projects/alpha.md", True),
        ("```\n[[search]]\n```", "wiki/index.md", False),  # code fence
        ("[[searching]]", "wiki/index.md", False),
        ("[ext](https://example.com/search.md)", "wiki/index.md", False),
    ],
)
def test_links_to_finds_links_that_resolve_to_a_file(
    text: str, source: str, expected: bool
) -> None:
    assert links_to(text, source, "wiki/concepts/search.md", FILES) is expected


def test_ambiguous_basename_resolves_to_the_file_in_the_same_folder_first() -> None:
    assert links_to("[[index]]", "projects/notes/other.md", "projects/notes/index.md", FILES)
    assert not links_to("[[index]]", "projects/notes/other.md", "wiki/index.md", FILES)


def test_rewrite_keeps_alias_heading_embed_and_link_style() -> None:
    text = (
        "a [[search]] b [[search|Find]] c [[search#Ranking|R]] d ![[search]]\n"
        "e [[wiki/concepts/search]] f [md](concepts/search.md#Top)\n"
        "```\n[[search]]\n```\n"
    )
    out = rewrite_links(
        text, "wiki/index.md", "wiki/concepts/search.md", "wiki/guides/finding.md", FILES
    )
    assert out == (
        "a [[finding]] b [[finding|Find]] c [[finding#Ranking|R]] d ![[finding]]\n"
        "e [[wiki/guides/finding]] f [md](guides/finding.md#Top)\n"
        "```\n[[search]]\n```\n"
    )


def test_rewrite_uses_the_full_path_when_the_new_basename_would_be_ambiguous() -> None:
    out = rewrite_links(
        "[[search]]", "projects/alpha.md", "wiki/concepts/search.md", "wiki/index2/index.md", FILES
    )
    assert out == "[[wiki/index2/index]]"


def test_relative_links_inside_the_moved_file_follow_it() -> None:
    from vault_mcp.links import rebase_relative_links

    text = "[a](../index.md) [b](https://x.y/z.md) [[alpha]]"
    out = rebase_relative_links(text, "wiki/concepts/search.md", "projects/search.md")
    assert out == "[a](../wiki/index.md) [b](https://x.y/z.md) [[alpha]]"
