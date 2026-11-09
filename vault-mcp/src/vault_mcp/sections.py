"""Section addressing for `replace_section` / `insert_after` (SPEC §6.4, plan D11).

Headings come from markdown-it-py's CommonMark token stream, so `#` lines inside code fences
are not headings and setext headings are. YAML frontmatter is stripped before parsing so its
closing `---` can't turn the line above it into a setext heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt

from vault_core import frontmatter
from vault_mcp.errors import VaultToolError

_MD = MarkdownIt("commonmark")
_ATX_PREFIX = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    start: int  # first line of the heading (0-based, whole file)
    body: int  # first line after the heading (after a setext underline)
    end: int  # first line after the section (next same-or-higher heading, or EOF)

    @property
    def label(self) -> str:
        return "#" * self.level + " " + self.text


def headings(text: str) -> list[Heading]:
    _, body = frontmatter.parse(text)
    offset = text[: len(text) - len(body)].count("\n")
    tokens = _MD.parse(body)
    found: list[tuple[int, str, int, int]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.map is not None:
            level = int(tok.tag[1])
            found.append(
                (level, tokens[i + 1].content.strip(), tok.map[0] + offset, tok.map[1] + offset)
            )
    total = len(text.splitlines(keepends=True))
    out: list[Heading] = []
    for idx, (level, label, start, body_start) in enumerate(found):
        end = next((s for lvl, _, s, _ in found[idx + 1 :] if lvl <= level), total)
        out.append(Heading(level, label, start, body_start, end))
    return out


def find(text: str, heading: str) -> Heading:
    """`heading` is the heading text ("Usage") or a full ATX line ("## Usage"), which also
    pins the level. It must match exactly one heading."""
    wanted = heading.strip()
    level: int | None = None
    if m := _ATX_PREFIX.match(wanted):
        level, wanted = len(m.group(1)), m.group(2)
    all_headings = headings(text)
    matches = [h for h in all_headings if h.text == wanted and (level is None or h.level == level)]
    if not matches:
        raise VaultToolError(
            "not_found",
            f"no heading {heading!r}",
            heading=heading,
            headings=[h.label for h in all_headings],
        )
    if len(matches) > 1:
        raise VaultToolError(
            "ambiguous_heading",
            f"{len(matches)} headings match {heading!r}; pass the full heading line or rename one",
            heading=heading,
            matches=[{"heading": h.label, "line": h.start + 1} for h in matches],
        )
    return matches[0]


def _ensure_newline(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"


def replace_section(text: str, heading: str, content: str) -> str:
    """Replace the section's body (everything under the heading, subsections included)."""
    h = find(text, heading)
    lines = text.splitlines(keepends=True)
    return "".join(lines[: h.body]) + _ensure_newline(content) + "".join(lines[h.end :])


def insert_after(text: str, heading: str, content: str) -> str:
    """Insert directly below the heading line, ahead of the existing body."""
    h = find(text, heading)
    lines = text.splitlines(keepends=True)
    return "".join(lines[: h.body]) + _ensure_newline(content) + "".join(lines[h.body :])
