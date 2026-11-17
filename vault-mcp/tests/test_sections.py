import json

import pytest
from fastmcp.exceptions import ToolError

from vault_mcp.sections import insert_after, replace_section

DOC = """\
---
title: Guide
---
# Guide

Intro.

## Install

Old install steps.

### Linux

Linux notes.

## Usage

```bash
## not a heading
```

Use it.
"""


def err(exc: pytest.ExceptionInfo[ToolError]) -> dict:  # type: ignore[type-arg]
    return json.loads(str(exc.value))


def test_replace_section_swaps_the_body_and_keeps_everything_else_byte_identical() -> None:
    out = replace_section(DOC, "Install", "New install steps.\n")
    before, after = DOC.split("## Install\n", 1)[0], DOC.split("## Usage\n", 1)[1]
    assert out.startswith(before + "## Install\nNew install steps.\n")
    assert out.endswith("## Usage\n" + after)
    assert "Linux notes" not in out  # subsections belong to the section


def test_section_ends_at_the_next_heading_of_the_same_or_higher_level() -> None:
    out = replace_section(DOC, "### Linux", "Replaced.\n")
    assert "### Linux\nReplaced.\n## Usage\n" in out
    assert "Old install steps." in out


def test_last_section_runs_to_the_end_of_the_file() -> None:
    out = replace_section(DOC, "Usage", "Just this.\n")
    assert out.endswith("## Usage\nJust this.\n")


def test_headings_inside_code_fences_are_ignored() -> None:
    with pytest.raises(ToolError) as exc:
        replace_section(DOC, "not a heading", "x")
    assert err(exc)["code"] == "not_found"


def test_frontmatter_is_not_mistaken_for_a_setext_heading() -> None:
    with pytest.raises(ToolError) as exc:
        replace_section(DOC, "title: Guide", "x")
    assert err(exc)["code"] == "not_found"
    assert err(exc)["details"]["headings"] == ["# Guide", "## Install", "### Linux", "## Usage"]


def test_setext_headings_are_supported() -> None:
    doc = "Title\n=====\n\nbody\n\nNext\n----\n\nmore\n"
    assert replace_section(doc, "Title", "new\n") == "Title\n=====\nnew\n"
    assert replace_section(doc, "Next", "x\n") == "Title\n=====\n\nbody\n\nNext\n----\nx\n"


def test_duplicate_headings_are_ambiguous_with_candidates() -> None:
    doc = "# A\n\n## Notes\n\none\n\n# B\n\n## Notes\n\ntwo\n"
    with pytest.raises(ToolError) as exc:
        replace_section(doc, "Notes", "x")
    e = err(exc)
    assert e["code"] == "ambiguous_heading"
    assert e["details"]["matches"] == [
        {"heading": "## Notes", "line": 3},
        {"heading": "## Notes", "line": 9},
    ]


def test_a_full_heading_line_selects_by_level() -> None:
    doc = "# Notes\n\ntop\n\n## Notes\n\nsub\n"
    assert replace_section(doc, "## Notes", "changed\n") == "# Notes\n\ntop\n\n## Notes\nchanged\n"


def test_content_gets_a_trailing_newline_and_crlf_outside_is_preserved() -> None:
    doc = "# A\r\n\r\nold\r\n# B\r\nkeep\r\n"
    assert replace_section(doc, "A", "new") == "# A\r\nnew\n# B\r\nkeep\r\n"


def test_insert_after_puts_content_directly_below_the_heading() -> None:
    out = insert_after(DOC, "Install", "Inserted first.\n")
    assert "## Install\nInserted first.\n\nOld install steps." in out
    assert out.replace("Inserted first.\n", "", 1) == DOC


def test_insert_after_a_setext_heading_goes_below_its_underline() -> None:
    assert insert_after("T\n=\nbody\n", "T", "x\n") == "T\n=\nx\nbody\n"
