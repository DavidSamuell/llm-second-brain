"""YAML frontmatter parsing. Unusable frontmatter is treated as absent, never as an error."""

from __future__ import annotations

from typing import Any

import yaml

_FENCE = "---"


def parse(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split `text` into (frontmatter mapping, body). Returns (None, text) when there is none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FENCE:
            try:
                meta = yaml.safe_load("".join(lines[1:i]))
            except yaml.YAMLError:
                return None, text
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                return None, text
            return meta, "".join(lines[i + 1 :])
    return None, text


def maintainer_of(text: str) -> str | None:
    """The page's `maintainer` value, lower-cased, or None when absent or not a string."""
    meta, _ = parse(text)
    value = meta.get("maintainer") if meta else None
    return value.strip().lower() if isinstance(value, str) else None
